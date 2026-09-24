package command

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestDouyinImportUsesRemoteAssetReceipt(t *testing.T) {
	var body map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/douyin/tasks" && r.Method == "POST" {
			json.NewDecoder(r.Body).Decode(&body)
			json.NewEncoder(w).Encode(map[string]any{"task_id": testMediaID, "status": "queued"})
			return
		}
		if r.URL.Path == "/api/douyin/tasks/"+testMediaID {
			json.NewEncoder(w).Encode(map[string]any{"task_id": testMediaID, "status": "completed", "mode": "download", "asset_id": testAssetID})
			return
		}
		t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
	}))
	defer server.Close()
	code, out, err := executeTest(t, []string{"--server", server.URL, "douyin", "import", "分享 https://v.douyin.com/abc/", "--quality", "720", "--json"}, "")
	if code != 0 || !strings.Contains(out, testAssetID) || body["quality"] != "720" || body["mode"] != "download" {
		t.Fatalf("code=%d out=%s err=%s body=%v", code, out, err, body)
	}
}
func TestDouyinRemoteHelpDoesNotConnect(t *testing.T) {
	code, out, err := executeTest(t, []string{"--server", "http://127.0.0.1:1", "douyin", "import", "--help"}, "")
	if code != 0 || !strings.Contains(out, "--local") {
		t.Fatalf("%d %s %s", code, out, err)
	}
}

func TestDouyinLocalImportUploadsVideoWithoutBrowserCookies(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fixture uses a POSIX shell")
	}
	fixture := filepath.Join(t.TempDir(), "fake-ytdlp")
	script := `#!/bin/sh
case " $* " in *" --cookies-from-browser chrome "*) ;; *) exit 9;; esac
case " $* " in
  *" --skip-download "*) printf '{"id":"123","title":"test","ext":"mp4"}\n' ;;
  *)
    while [ "$1" != "-o" ]; do shift; done
    path=$(printf '%s' "$2" | sed 's/%(ext)s/mp4/')
    printf 'video' > "$path"
    printf '%s\n' "$path" ;;
esac
`
	if err := os.WriteFile(fixture, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	var uploaded string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/assets" {
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
			return
		}
		if r.Header.Get("Cookie") != "" {
			t.Error("browser cookie was sent to Studio")
		}
		file, _, err := r.FormFile("file")
		if err != nil {
			t.Error(err)
			return
		}
		defer file.Close()
		data := make([]byte, 5)
		if _, err := file.Read(data); err != nil {
			t.Error(err)
		}
		uploaded = string(data)
		json.NewEncoder(w).Encode(map[string]any{"asset_id": testAssetID, "kind": "video"})
	}))
	defer server.Close()
	code, out, stderr := executeTest(t, []string{"--server", server.URL, "douyin", "import", "https://v.douyin.com/abc/", "--local", "--cookies-from-browser", "chrome", "--yt-dlp", fixture, "--json"}, "")
	if code != 0 || uploaded != "video" || !strings.Contains(out, testAssetID) {
		t.Fatalf("code=%d uploaded=%q out=%s stderr=%s", code, uploaded, out, stderr)
	}
}

func TestCLIVersionMatchesPackageVersion(t *testing.T) {
	data, err := os.ReadFile("../../../package.json")
	if err != nil {
		t.Fatal(err)
	}
	var pkg struct {
		Version string `json:"version"`
	}
	if err = json.Unmarshal(data, &pkg); err != nil {
		t.Fatal(err)
	}
	if Version != pkg.Version {
		t.Fatalf("CLI %s != package %s", Version, pkg.Version)
	}
}
