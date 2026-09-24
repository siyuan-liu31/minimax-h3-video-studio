package douyin

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const maxProcessOutput = 8 << 20

var linkPattern = regexp.MustCompile(`https://[^\s]+`)

type Error struct {
	Code      string
	Message   string
	Retryable bool
	Cause     error
}

func (e *Error) Error() string { return e.Message }
func (e *Error) Unwrap() error { return e.Cause }

type Format struct {
	ID       string `json:"id,omitempty"`
	Ext      string `json:"ext,omitempty"`
	Width    int    `json:"width,omitempty"`
	Height   int    `json:"height,omitempty"`
	VCodec   string `json:"vcodec,omitempty"`
	ACodec   string `json:"acodec,omitempty"`
	FileSize int64  `json:"filesize,omitempty"`
}

type Metadata struct {
	ID         string   `json:"id"`
	Title      string   `json:"title,omitempty"`
	Uploader   string   `json:"uploader,omitempty"`
	Duration   float64  `json:"duration,omitempty"`
	Thumbnail  string   `json:"thumbnail,omitempty"`
	WebpageURL string   `json:"webpage_url,omitempty"`
	Width      int      `json:"width,omitempty"`
	Height     int      `json:"height,omitempty"`
	Ext        string   `json:"ext,omitempty"`
	Formats    []Format `json:"formats,omitempty"`
}

type DownloadResult struct {
	Path   string `json:"path,omitempty"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256,omitempty"`
}

type Extractor interface {
	Parse(context.Context, string) (Metadata, error)
	Download(context.Context, string, string, bool) (DownloadResult, error)
}

type CommandResult struct {
	Stdout string
	Stderr string
}

type CommandRunner func(context.Context, string, ...string) (CommandResult, error)

type Config struct {
	Executable         string
	CookiesFromBrowser string
	Timeout            time.Duration
	Run                CommandRunner
}

type Client struct {
	executable string
	browser    string
	timeout    time.Duration
	run        CommandRunner
}

func New(config Config) (*Client, error) {
	executable, err := resolveExecutable(config.Executable)
	if err != nil {
		return nil, err
	}
	if err := validateBrowser(config.CookiesFromBrowser); err != nil {
		return nil, err
	}
	if config.Timeout <= 0 {
		config.Timeout = 90 * time.Second
	}
	run := config.Run
	if run == nil {
		run = runCommand
	}
	return &Client{executable: executable, browser: config.CookiesFromBrowser, timeout: config.Timeout, run: run}, nil
}

func ExtractURL(text string) (string, error) {
	raw := linkPattern.FindString(strings.TrimSpace(text))
	raw = strings.TrimRight(raw, `,.。，!?！？;；:)）]}》」"'`)
	if raw == "" {
		return "", &Error{Code: "invalid_link", Message: "no HTTPS link was found in the supplied text"}
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.User != nil || parsed.Port() != "" {
		return "", &Error{Code: "invalid_link", Message: "the Douyin link is invalid"}
	}
	host := strings.ToLower(parsed.Hostname())
	allowed := map[string]bool{
		"douyin.com": true, "www.douyin.com": true, "m.douyin.com": true,
		"v.douyin.com": true, "iesdouyin.com": true, "www.iesdouyin.com": true,
	}
	if !allowed[host] {
		return "", &Error{Code: "invalid_link", Message: "only public douyin.com and iesdouyin.com links are accepted"}
	}
	parsed.Fragment = ""
	return parsed.String(), nil
}

func (c *Client) Parse(ctx context.Context, text string) (Metadata, error) {
	link, err := ExtractURL(text)
	if err != nil {
		return Metadata{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	args := c.baseArgs()
	args = append(args, "--skip-download", "--dump-single-json", "--", link)
	result, runErr := c.run(ctx, c.executable, args...)
	if runErr != nil {
		return Metadata{}, classifyProcessError(ctx, result.Stderr, runErr)
	}
	var raw struct {
		ID         string  `json:"id"`
		Title      string  `json:"title"`
		Uploader   string  `json:"uploader"`
		Thumbnail  string  `json:"thumbnail"`
		WebpageURL string  `json:"webpage_url"`
		Ext        string  `json:"ext"`
		Duration   float64 `json:"duration"`
		Width      int     `json:"width"`
		Height     int     `json:"height"`
		Formats    []struct {
			FormatID string `json:"format_id"`
			Ext      string `json:"ext"`
			VCodec   string `json:"vcodec"`
			ACodec   string `json:"acodec"`
			Width    int    `json:"width"`
			Height   int    `json:"height"`
			FileSize int64  `json:"filesize"`
		}
	}
	if err := json.Unmarshal([]byte(result.Stdout), &raw); err != nil {
		return Metadata{}, &Error{Code: "invalid_response", Message: "yt-dlp returned invalid metadata", Cause: err}
	}
	if raw.ID == "" {
		return Metadata{}, &Error{Code: "invalid_response", Message: "yt-dlp metadata did not include a video id"}
	}
	metadata := Metadata{
		ID: raw.ID, Title: raw.Title, Uploader: raw.Uploader, Duration: raw.Duration,
		Thumbnail: raw.Thumbnail, WebpageURL: raw.WebpageURL, Width: raw.Width, Height: raw.Height, Ext: raw.Ext,
	}
	for _, item := range raw.Formats {
		if item.VCodec == "none" {
			continue
		}
		metadata.Formats = append(metadata.Formats, Format{
			ID: item.FormatID, Ext: item.Ext, Width: item.Width, Height: item.Height,
			VCodec: item.VCodec, ACodec: item.ACodec, FileSize: item.FileSize,
		})
	}
	return metadata, nil
}

func (c *Client) Download(ctx context.Context, text, outputTemplate string, force bool) (DownloadResult, error) {
	link, err := ExtractURL(text)
	if err != nil {
		return DownloadResult{}, err
	}
	if strings.TrimSpace(outputTemplate) == "" || strings.ContainsAny(outputTemplate, "\x00\r\n") {
		return DownloadResult{}, &Error{Code: "invalid_output", Message: "an output path or template is required"}
	}
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	args := c.baseArgs()
	args = append(args, "--no-part", "--print", "after_move:filepath", "-o", outputTemplate)
	if force {
		args = append(args, "--force-overwrites")
	}
	args = append(args, "--", link)
	result, runErr := c.run(ctx, c.executable, args...)
	if runErr != nil {
		return DownloadResult{}, classifyProcessError(ctx, result.Stderr, runErr)
	}
	lines := strings.Split(strings.TrimSpace(result.Stdout), "\n")
	path := strings.TrimSpace(lines[len(lines)-1])
	if path == "" {
		return DownloadResult{}, &Error{Code: "invalid_response", Message: "yt-dlp did not report the downloaded file path"}
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return DownloadResult{}, &Error{Code: "invalid_response", Message: "yt-dlp returned an invalid file path", Cause: err}
	}
	info, err := os.Stat(abs)
	if err != nil || !info.Mode().IsRegular() {
		return DownloadResult{}, &Error{Code: "download_missing", Message: "yt-dlp completed without creating a regular media file", Cause: err}
	}
	digest, err := fileSHA256(abs)
	if err != nil {
		return DownloadResult{}, &Error{Code: "download_hash_failed", Message: "downloaded media could not be hashed", Cause: err}
	}
	return DownloadResult{Path: abs, Size: info.Size(), SHA256: digest}, nil
}

func (c *Client) baseArgs() []string {
	args := []string{
		"--ignore-config", "--quiet", "--no-warnings", "--no-playlist",
		"--socket-timeout", strconv.Itoa(max(10, int(c.timeout.Seconds()/3))),
		"--retries", "3", "--extractor-retries", "3", "--retry-sleep", "extractor:1",
	}
	if c.browser != "" {
		args = append(args, "--cookies-from-browser", c.browser)
	}
	return args
}

func resolveExecutable(explicit string) (string, error) {
	candidate := strings.TrimSpace(explicit)
	if candidate == "" {
		candidate = strings.TrimSpace(os.Getenv("H3CTL_YTDLP"))
	}
	if candidate == "" {
		candidate = "yt-dlp"
	}
	path, err := exec.LookPath(candidate)
	if err != nil {
		return "", &Error{Code: "dependency_missing", Message: "yt-dlp was not found; install it or pass --yt-dlp PATH", Cause: err}
	}
	return path, nil
}

func validateBrowser(value string) error {
	if len(value) > 256 || strings.ContainsAny(value, "\x00\r\n") {
		return &Error{Code: "invalid_browser", Message: "--cookies-from-browser contains invalid characters"}
	}
	return nil
}

func runCommand(ctx context.Context, executable string, args ...string) (CommandResult, error) {
	cmd := exec.CommandContext(ctx, executable, args...)
	var stdout, stderr cappedBuffer
	stdout.limit, stderr.limit = maxProcessOutput, maxProcessOutput
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err := cmd.Run()
	if stdout.exceeded || stderr.exceeded {
		return CommandResult{Stdout: stdout.String(), Stderr: stderr.String()}, &Error{Code: "output_too_large", Message: "yt-dlp produced too much output"}
	}
	return CommandResult{Stdout: stdout.String(), Stderr: stderr.String()}, err
}

func classifyProcessError(ctx context.Context, stderr string, err error) error {
	lower := strings.ToLower(stderr)
	switch {
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return &Error{Code: "timeout", Message: "Douyin parsing timed out", Retryable: true, Cause: err}
	case strings.Contains(lower, "http error 429") || strings.Contains(lower, "too many requests"):
		return &Error{Code: "rate_limited", Message: "Douyin is limiting requests. Stop repeated attempts, verify the video in the selected browser, and retry later", Retryable: true, Cause: err}
	case strings.Contains(lower, "fresh cookies") || strings.Contains(lower, "cookies are needed") || strings.Contains(lower, "sign in"):
		return &Error{Code: "cookie_refresh_required", Message: "Douyin rejected the request. Verify that the video plays in the selected browser, then retry later; this message does not prove that your cookies expired", Retryable: true, Cause: err}
	case strings.Contains(lower, "http error 403") || strings.Contains(lower, "403 forbidden"):
		return &Error{Code: "access_restricted", Message: "Douyin refused video access (HTTP 403). Verify the video in the selected browser and retry later; the response does not identify whether the account, session, or request was restricted", Retryable: true, Cause: err}
	case strings.Contains(lower, "unsupported url") || strings.Contains(lower, "invalid url"):
		return &Error{Code: "invalid_link", Message: "the supplied Douyin link could not be parsed", Cause: err}
	default:
		message := strings.TrimSpace(stderr)
		if message == "" {
			message = "yt-dlp failed to parse the Douyin link"
		}
		if len(message) > 1000 {
			message = message[:1000]
		}
		return &Error{Code: "extractor_failed", Message: message, Retryable: true, Cause: err}
	}
}

type cappedBuffer struct {
	buffer   bytes.Buffer
	limit    int
	exceeded bool
}

func (w *cappedBuffer) Write(value []byte) (int, error) {
	original := len(value)
	remaining := w.limit - w.buffer.Len()
	if remaining <= 0 {
		w.exceeded = true
		return original, nil
	}
	if len(value) > remaining {
		value = value[:remaining]
		w.exceeded = true
	}
	_, _ = w.buffer.Write(value)
	return original, nil
}

func (w *cappedBuffer) String() string { return w.buffer.String() }

func fileSHA256(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", digest.Sum(nil)), nil
}

func MapError(err error) *Error {
	var typed *Error
	if errors.As(err, &typed) {
		return typed
	}
	return &Error{Code: "internal_error", Message: fmt.Sprintf("Douyin operation failed: %v", err), Cause: err}
}
