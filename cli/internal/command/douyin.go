package command

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/douyin"
)

const DouyinHelp = `Usage: h3ctl douyin COMMAND

  import TEXT [--quality best|1080|720] [--detach] [--timeout 10m]
  import TEXT --local [--cookies-from-browser BROWSER] [--yt-dlp PATH]
  inspect TEXT [--detach]                 Parse on the Studio server
  capabilities | list | status TASK | wait TASK | cancel TASK | retry TASK

  parse TEXT [--cookies-from-browser BROWSER] [--yt-dlp PATH]
  download TEXT [--to PATH] [--force] [--cookies-from-browser BROWSER] [--yt-dlp PATH]
  serve [--listen 127.0.0.1:8765] [--data-dir PATH] [--cache-ttl 1h]
        [--rate-limit 30] [--cookies-from-browser BROWSER] [--yt-dlp PATH]
        [--studio-origin http://127.0.0.1:16020]

parse/download/serve run locally without opening the H3 context.
import/inspect and task commands use the selected Studio server. import --local
downloads locally and uploads into that server asset library. TEXT may be a public Douyin HTTPS URL or the complete copied share text.
Browser cookies are read only when --cookies-from-browser is supplied; they are
passed directly to yt-dlp and are never stored by h3ctl. The API server is
loopback-only and exposes Swagger UI at /docs.

Examples:
  h3ctl douyin parse 'https://v.douyin.com/...' --cookies-from-browser chrome --json
  h3ctl douyin download 'share text https://v.douyin.com/...' --to ./downloads \
    --cookies-from-browser chrome
  h3ctl douyin serve --cookies-from-browser chrome
  h3ctl douyin serve --studio-origin http://127.0.0.1:16020 --cookies-from-browser chrome
`

func (r *Runner) runDouyin(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, DouyinHelp)
		return nil, nil
	}
	action := args[0]
	switch action {
	case "import", "inspect", "capabilities", "list", "status", "wait", "cancel", "retry":
		return r.runDouyinRemote(ctx, args)
	case "parse":
		set := newFlags("douyin parse")
		browser := set.String("cookies-from-browser", "", "browser cookie source understood by yt-dlp")
		executable := set.String("yt-dlp", "", "yt-dlp executable path")
		timeout := set.Duration("timeout", 90*time.Second, "complete parse timeout")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() == 0 || *timeout <= 0 {
			return nil, usage("douyin parse requires share TEXT and a positive --timeout")
		}
		client, err := douyin.New(douyin.Config{Executable: *executable, CookiesFromBrowser: *browser, Timeout: *timeout})
		if err != nil {
			return nil, douyinCLIError(err)
		}
		metadata, err := client.Parse(ctx, strings.Join(set.Args(), " "))
		if err != nil {
			return nil, douyinCLIError(err)
		}
		return metadata, nil

	case "download":
		set := newFlags("douyin download")
		to := set.String("to", ".", "destination file, template, or directory")
		force := set.Bool("force", false, "overwrite an existing exact destination")
		browser := set.String("cookies-from-browser", "", "browser cookie source understood by yt-dlp")
		executable := set.String("yt-dlp", "", "yt-dlp executable path")
		timeout := set.Duration("timeout", 5*time.Minute, "complete parse or download timeout")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() == 0 || *timeout <= 0 {
			return nil, usage("douyin download requires share TEXT and a positive --timeout")
		}
		template, err := douyinOutputTemplate(*to, *force)
		if err != nil {
			return nil, err
		}
		client, err := douyin.New(douyin.Config{Executable: *executable, CookiesFromBrowser: *browser, Timeout: *timeout})
		if err != nil {
			return nil, douyinCLIError(err)
		}
		text := strings.Join(set.Args(), " ")
		metadata, err := client.Parse(ctx, text)
		if err != nil {
			return nil, douyinCLIError(err)
		}
		download, err := client.Download(ctx, text, template, *force)
		if err != nil {
			return nil, douyinCLIError(err)
		}
		return map[string]any{"metadata": metadata, "download": download}, nil

	case "serve":
		set := newFlags("douyin serve")
		listenAddress := set.String("listen", "127.0.0.1:8765", "loopback listen address")
		dataDir := set.String("data-dir", "", "cache directory")
		cacheTTL := set.Duration("cache-ttl", time.Hour, "completed download lifetime")
		rateLimit := set.Int("rate-limit", 30, "parse requests per client IP per minute")
		studioOrigin := set.String("studio-origin", "", "allow this loopback Studio origin to use the token-protected local bridge")
		browser := set.String("cookies-from-browser", "", "browser cookie source understood by yt-dlp")
		executable := set.String("yt-dlp", "", "yt-dlp executable path")
		timeout := set.Duration("timeout", 5*time.Minute, "per parse or download timeout")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 || *cacheTTL <= 0 || *rateLimit <= 0 || *timeout <= 0 {
			return nil, usage("douyin serve accepts no positional arguments and requires positive timeout, cache TTL, and rate limit")
		}
		if err := douyin.ValidateListenAddress(*listenAddress); err != nil {
			return nil, usage("%v", err)
		}
		client, err := douyin.New(douyin.Config{Executable: *executable, CookiesFromBrowser: *browser, Timeout: *timeout})
		if err != nil {
			return nil, douyinCLIError(err)
		}
		api, err := douyin.NewAPI(client, douyin.APIConfig{DataDir: *dataDir, TTL: *cacheTTL, RateLimit: *rateLimit, StudioOrigin: *studioOrigin})
		if err != nil {
			return nil, &contract.CLIError{Code: "server_start_failed", Message: err.Error(), Cause: err}
		}
		defer api.Close()
		listener, err := net.Listen("tcp", *listenAddress)
		if err != nil {
			return nil, &contract.CLIError{Code: "server_start_failed", Message: err.Error(), Cause: err}
		}
		defer listener.Close()
		if !r.Globals.Quiet {
			fmt.Fprintf(r.Streams.Err, "Douyin API: http://%s\nSwagger: http://%s/docs\n", listener.Addr(), listener.Addr())
			if *studioOrigin != "" {
				fmt.Fprintf(r.Streams.Err, "Studio local bridge enabled for %s\n", *studioOrigin)
			}
		}
		if err := api.Serve(ctx, listener); err != nil {
			return nil, &contract.CLIError{Code: "server_failed", Message: err.Error(), Cause: err}
		}
		return nil, nil
	default:
		return nil, usage("unknown douyin command %q", action)
	}
}

func douyinOutputTemplate(destination string, force bool) (string, error) {
	if strings.TrimSpace(destination) == "" || strings.ContainsAny(destination, "\x00\r\n") {
		return "", usage("--to must be a safe local path")
	}
	abs, err := filepath.Abs(destination)
	if err != nil {
		return "", usage("invalid --to path: %v", err)
	}
	info, statErr := os.Stat(abs)
	if statErr == nil && info.IsDir() {
		return filepath.Join(abs, "%(uploader).80B-%(id)s.%(ext)s"), nil
	}
	if statErr != nil && !errors.Is(statErr, os.ErrNotExist) {
		return "", &contract.CLIError{Code: "output_error", Message: statErr.Error(), Cause: statErr}
	}
	if strings.HasSuffix(destination, string(filepath.Separator)) {
		if err := os.MkdirAll(abs, 0o755); err != nil {
			return "", &contract.CLIError{Code: "output_error", Message: err.Error(), Cause: err}
		}
		return filepath.Join(abs, "%(uploader).80B-%(id)s.%(ext)s"), nil
	}
	if !strings.Contains(abs, "%(") && filepath.Ext(abs) == "" {
		abs += ".%(ext)s"
	}
	if !strings.Contains(abs, "%(") && !force {
		if _, err := os.Stat(abs); err == nil {
			return "", &contract.CLIError{Code: "conflict", Message: "destination already exists; pass --force to overwrite"}
		}
	}
	parent := filepath.Dir(abs)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return "", &contract.CLIError{Code: "output_error", Message: err.Error(), Cause: err}
	}
	return abs, nil
}

func douyinCLIError(err error) error {
	typed := douyin.MapError(err)
	return &contract.CLIError{Code: typed.Code, Message: typed.Message, Retryable: typed.Retryable, Cause: err}
}
