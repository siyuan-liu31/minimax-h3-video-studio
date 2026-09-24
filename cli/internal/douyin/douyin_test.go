package douyin

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestExtractURLAcceptsShareTextAndRejectsUnsafeHosts(t *testing.T) {
	link, err := ExtractURL("1.23 复制打开抖音 https://v.douyin.com/abc-123/ :9pm")
	if err != nil || link != "https://v.douyin.com/abc-123/" {
		t.Fatalf("link=%q err=%v", link, err)
	}
	for _, value := range []string{
		"http://v.douyin.com/abc", "https://v.douyin.com.evil.example/abc",
		"https://127.0.0.1/video/1", "https://user@v.douyin.com/video/1",
		"https://v.douyin.com:443/video/1", "no link",
	} {
		if _, err := ExtractURL(value); err == nil {
			t.Fatalf("unsafe value accepted: %s", value)
		}
	}
}

func TestClientParseUsesExplicitBrowserAndSanitizesFormats(t *testing.T) {
	executable, _ := os.Executable()
	var received []string
	client, err := New(Config{
		Executable: executable, CookiesFromBrowser: "chrome:Default", Timeout: time.Minute,
		Run: func(_ context.Context, _ string, args ...string) (CommandResult, error) {
			received = append([]string(nil), args...)
			return CommandResult{Stdout: `{
              "id":"123","title":"title","uploader":"author","duration":13.1,
              "webpage_url":"https://www.douyin.com/video/123","width":540,"height":1004,"ext":"mp4",
              "formats":[
                {"format_id":"audio","vcodec":"none","acodec":"aac"},
                {"format_id":"video","ext":"mp4","width":540,"height":1004,"vcodec":"hevc","acodec":"aac","filesize":10}
              ]}`}, nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	metadata, err := client.Parse(context.Background(), "https://v.douyin.com/abc/")
	if err != nil {
		t.Fatal(err)
	}
	if metadata.ID != "123" || metadata.WebpageURL == "" || len(metadata.Formats) != 1 || metadata.Formats[0].VCodec != "hevc" {
		t.Fatalf("metadata=%#v", metadata)
	}
	if !slices.Contains(received, "--ignore-config") || !containsPair(received, "--cookies-from-browser", "chrome:Default") {
		t.Fatalf("args=%q", received)
	}
	if received[len(received)-2] != "--" || received[len(received)-1] != "https://v.douyin.com/abc/" {
		t.Fatalf("URL was not safely separated: %q", received)
	}
}

func TestClientDownloadReturnsVerifiedFileAndHash(t *testing.T) {
	executable, _ := os.Executable()
	directory := t.TempDir()
	client, err := New(Config{
		Executable: executable,
		Run: func(_ context.Context, _ string, args ...string) (CommandResult, error) {
			template := pairValue(args, "-o")
			path := strings.ReplaceAll(template, "%(.ext)s", "mp4")
			path = strings.ReplaceAll(path, "%(ext)s", "mp4")
			if err := os.WriteFile(path, []byte("video"), 0o600); err != nil {
				return CommandResult{}, err
			}
			return CommandResult{Stdout: path + "\n"}, nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.Download(context.Background(), "https://v.douyin.com/abc/", filepath.Join(directory, "clip.%(ext)s"), false)
	if err != nil {
		t.Fatal(err)
	}
	if result.Size != 5 || len(result.SHA256) != 64 || !strings.HasSuffix(result.Path, "clip.mp4") {
		t.Fatalf("result=%#v", result)
	}
}

func TestClientClassifiesCookieRefresh(t *testing.T) {
	executable, _ := os.Executable()
	client, err := New(Config{Executable: executable, Run: func(context.Context, string, ...string) (CommandResult, error) {
		return CommandResult{Stderr: "Fresh cookies (not necessarily logged in) are needed"}, errors.New("exit 1")
	}})
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.Parse(context.Background(), "https://v.douyin.com/abc/")
	typed := MapError(err)
	if typed.Code != "cookie_refresh_required" || !typed.Retryable {
		t.Fatalf("error=%#v", typed)
	}
}

func TestClientDistinguishesAccessRefusalAndRateLimit(t *testing.T) {
	for _, test := range []struct {
		stderr string
		code   string
	}{
		{"ERROR: HTTP Error 403: Forbidden", "access_restricted"},
		{"ERROR: HTTP Error 429: Too Many Requests", "rate_limited"},
	} {
		t.Run(test.code, func(t *testing.T) {
			err := classifyProcessError(context.Background(), test.stderr, errors.New("exit 1"))
			actual := MapError(err)
			if actual.Code != test.code || !actual.Retryable {
				t.Fatalf("error=%#v", actual)
			}
		})
	}
}

func containsPair(values []string, name, value string) bool {
	for index := 0; index+1 < len(values); index++ {
		if values[index] == name && values[index+1] == value {
			return true
		}
	}
	return false
}

func pairValue(values []string, name string) string {
	for index := 0; index+1 < len(values); index++ {
		if values[index] == name {
			return values[index+1]
		}
	}
	return ""
}
