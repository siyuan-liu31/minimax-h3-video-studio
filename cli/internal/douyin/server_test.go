package douyin

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type fakeExtractor struct {
	parseCalls    atomic.Int32
	downloadCalls atomic.Int32
	fail          error
}

func (f *fakeExtractor) Parse(context.Context, string) (Metadata, error) {
	f.parseCalls.Add(1)
	if f.fail != nil {
		return Metadata{}, f.fail
	}
	return Metadata{ID: "123", Title: "title", Uploader: "author", Duration: 13}, nil
}

func (f *fakeExtractor) Download(_ context.Context, _ string, template string, _ bool) (DownloadResult, error) {
	f.downloadCalls.Add(1)
	if f.fail != nil {
		return DownloadResult{}, f.fail
	}
	path := strings.ReplaceAll(template, "%(ext)s", "mp4")
	content := []byte("0123456789")
	if err := os.WriteFile(path, content, 0o600); err != nil {
		return DownloadResult{}, err
	}
	return DownloadResult{Path: path, Size: int64(len(content)), SHA256: strings.Repeat("a", 64)}, nil
}

func TestAPICompletesTaskCachesAndServesRange(t *testing.T) {
	extractor := &fakeExtractor{}
	api, err := NewAPI(extractor, APIConfig{DataDir: t.TempDir(), TTL: time.Hour, RateLimit: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	server := httptest.NewServer(api.Handler())
	defer server.Close()

	task := submitTask(t, server.URL, "https://v.douyin.com/abc/")
	if task.ExpiresAt != nil {
		t.Fatalf("pending task unexpectedly has an expiry: %v", task.ExpiresAt)
	}
	task = waitTask(t, server.URL, task.ID)
	if task.Status != "completed" || task.DownloadURL == "" || task.Download == nil || task.Download.Path != "" {
		t.Fatalf("task=%#v", task)
	}
	if extractor.parseCalls.Load() != 1 || extractor.downloadCalls.Load() != 1 {
		t.Fatalf("calls parse=%d download=%d", extractor.parseCalls.Load(), extractor.downloadCalls.Load())
	}

	request, _ := http.NewRequest(http.MethodGet, server.URL+task.DownloadURL, nil)
	request.Header.Set("Range", "bytes=2-5")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(response.Body)
	response.Body.Close()
	if response.StatusCode != http.StatusPartialContent || string(body) != "2345" {
		t.Fatalf("status=%d body=%q", response.StatusCode, body)
	}

	second := submitTask(t, server.URL, "https://v.douyin.com/abc/")
	if second.ID != task.ID || extractor.parseCalls.Load() != 1 {
		t.Fatalf("cache miss second=%#v calls=%d", second, extractor.parseCalls.Load())
	}
}

func TestRetryableFailureCanBeRetriedAfterCooldown(t *testing.T) {
	var clock atomic.Int64
	clock.Store(time.Now().UnixNano())
	extractor := &fakeExtractor{fail: &Error{Code: "cookie_refresh_required", Message: "refresh session", Retryable: true}}
	api, err := NewAPI(extractor, APIConfig{DataDir: t.TempDir(), TTL: time.Hour, Now: func() time.Time { return time.Unix(0, clock.Load()) }})
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	server := httptest.NewServer(api.Handler())
	defer server.Close()
	first := waitTask(t, server.URL, submitTask(t, server.URL, "https://v.douyin.com/abc/").ID)
	if first.Status != "failed" || first.ExpiresAt == nil || first.ExpiresAt.Sub(first.UpdatedAt) != time.Minute {
		t.Fatalf("retryable failure expiry=%#v", first)
	}
	if cached := submitTask(t, server.URL, first.SourceURL); cached.ID != first.ID {
		t.Fatal("failure was retried inside cooldown")
	}
	clock.Add(int64(61 * time.Second))
	if retried := submitTask(t, server.URL, first.SourceURL); retried.ID == first.ID {
		t.Fatal("retryable failure remained cached after cooldown")
	}
}

func TestAPIRejectsSSRFAndRateLimits(t *testing.T) {
	extractor := &fakeExtractor{}
	api, err := NewAPI(extractor, APIConfig{DataDir: t.TempDir(), RateLimit: 1})
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	server := httptest.NewServer(api.Handler())
	defer server.Close()

	response, _ := http.Post(server.URL+"/api/parse", "application/json", strings.NewReader(`{"text":"https://127.0.0.1/private"}`))
	if response.StatusCode != http.StatusBadRequest {
		t.Fatalf("SSRF status=%d", response.StatusCode)
	}
	response.Body.Close()
	response, _ = http.Post(server.URL+"/api/parse", "application/json", strings.NewReader(`{"text":"https://v.douyin.com/abc/"}`))
	if response.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("rate status=%d", response.StatusCode)
	}
	response.Body.Close()
	if extractor.parseCalls.Load() != 0 {
		t.Fatal("rejected requests reached extractor")
	}
}

func TestDocsAndOpenAPI(t *testing.T) {
	api, err := NewAPI(&fakeExtractor{}, APIConfig{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	server := httptest.NewServer(api.Handler())
	defer server.Close()
	for path, marker := range map[string]string{"/docs": "SwaggerUIBundle", "/openapi.json": `"openapi":"3.1.0"`} {
		response, err := http.Get(server.URL + path)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(response.Body)
		response.Body.Close()
		if response.StatusCode != http.StatusOK || !bytes.Contains(body, []byte(marker)) {
			t.Fatalf("path=%s status=%d body=%s", path, response.StatusCode, body)
		}
	}
	spec := openAPISpec()
	components := spec["components"].(map[string]any)["schemas"].(map[string]any)
	for _, name := range []string{"ParseRequest", "ParseResponse", "InspectResponse", "Task", "Metadata", "DownloadResult", "ErrorResponse"} {
		if components[name] == nil {
			t.Fatalf("OpenAPI schema %q is missing", name)
		}
	}
}

func TestExpiredTaskDeletesOnlyManagedFile(t *testing.T) {
	now := time.Now()
	directory := t.TempDir()
	api, err := NewAPI(&fakeExtractor{}, APIConfig{DataDir: directory, TTL: time.Second, Now: func() time.Time { return now }})
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	task, _, err := api.start("https://v.douyin.com/abc/")
	if err != nil {
		t.Fatal(err)
	}
	completed := false
	for deadline := time.Now().Add(time.Second); time.Now().Before(deadline); {
		api.mu.RLock()
		current := cloneTask(api.tasks[task.ID])
		api.mu.RUnlock()
		if current != nil && current.Status == "completed" {
			task = current
			completed = true
			break
		}
		time.Sleep(time.Millisecond)
	}
	if !completed {
		t.Fatal("task did not complete before cleanup test deadline")
	}
	outside := filepath.Join(t.TempDir(), "keep.mp4")
	_ = os.WriteFile(outside, []byte("keep"), 0o600)
	now = now.Add(2 * time.Second)
	api.cleanupExpired()
	if _, err := os.Stat(task.filePath); !os.IsNotExist(err) {
		t.Fatalf("managed file was not deleted: %v", err)
	}
	if _, err := os.Stat(outside); err != nil {
		t.Fatalf("outside file was touched: %v", err)
	}
}

func TestValidateListenAddressIsLoopbackOnly(t *testing.T) {
	for _, value := range []string{"127.0.0.1:8765", "[::1]:8765", "localhost:8765"} {
		if err := ValidateListenAddress(value); err != nil {
			t.Fatalf("%s: %v", value, err)
		}
	}
	for _, value := range []string{"0.0.0.0:8765", "192.168.1.2:8765", "bad"} {
		if err := ValidateListenAddress(value); err == nil {
			t.Fatalf("unsafe listen accepted: %s", value)
		}
	}
}

func TestStudioBridgeAllowsOnlyPairedLoopbackOrigin(t *testing.T) {
	origin := "http://127.0.0.1:16020"
	token := strings.Repeat("a", 48)
	extractor := &fakeExtractor{}
	api, err := NewAPI(extractor, APIConfig{DataDir: t.TempDir(), StudioOrigin: origin, AccessToken: token})
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	server := httptest.NewServer(api.Handler())
	defer server.Close()

	health, _ := http.NewRequest(http.MethodGet, server.URL+"/health", nil)
	health.Header.Set("Origin", origin)
	response, err := http.DefaultClient.Do(health)
	if err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	_ = json.NewDecoder(response.Body).Decode(&body)
	response.Body.Close()
	if response.StatusCode != http.StatusOK || body["bridge_token"] != token || response.Header.Get("Access-Control-Allow-Origin") != origin {
		t.Fatalf("health status=%d body=%v CORS=%q", response.StatusCode, body, response.Header.Get("Access-Control-Allow-Origin"))
	}

	preflight, _ := http.NewRequest(http.MethodOptions, server.URL+"/api/parse", nil)
	preflight.Header.Set("Origin", origin)
	preflight.Header.Set("Access-Control-Request-Method", "POST")
	response, _ = http.DefaultClient.Do(preflight)
	response.Body.Close()
	if response.StatusCode != http.StatusNoContent {
		t.Fatalf("preflight status=%d", response.StatusCode)
	}

	for _, test := range []struct {
		name, origin, token string
		status              int
	}{
		{"wrong origin", "https://attacker.example", token, http.StatusForbidden},
		{"missing token", origin, "", http.StatusUnauthorized},
		{"wrong token", origin, strings.Repeat("b", 48), http.StatusUnauthorized},
		{"paired request", origin, token, http.StatusAccepted},
	} {
		t.Run(test.name, func(t *testing.T) {
			if test.name == "paired request" && extractor.parseCalls.Load() != 0 {
				t.Fatal("unauthorized request reached extractor")
			}
			request, _ := http.NewRequest(http.MethodPost, server.URL+"/api/parse", strings.NewReader(`{"text":"https://v.douyin.com/abc/"}`))
			request.Header.Set("Origin", test.origin)
			request.Header.Set("X-H3-Douyin-Token", test.token)
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			response.Body.Close()
			if response.StatusCode != test.status {
				t.Fatalf("status=%d, want=%d", response.StatusCode, test.status)
			}
		})
	}
}

func TestInspectReturnsMetadataWithoutDownloading(t *testing.T) {
	extractor := &fakeExtractor{}
	api, err := NewAPI(extractor, APIConfig{DataDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	defer api.Close()
	server := httptest.NewServer(api.Handler())
	defer server.Close()
	response, err := http.Post(server.URL+"/api/inspect", "application/json", strings.NewReader(`{"text":"https://v.douyin.com/abc/"}`))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var body struct {
		Metadata Metadata `json:"metadata"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || body.Metadata.ID != "123" || extractor.parseCalls.Load() != 1 || extractor.downloadCalls.Load() != 0 {
		t.Fatalf("status=%d metadata=%#v parse=%d download=%d", response.StatusCode, body.Metadata, extractor.parseCalls.Load(), extractor.downloadCalls.Load())
	}
}

func TestStudioBridgeRejectsRemoteOrigin(t *testing.T) {
	for _, origin := range []string{"https://127.0.0.1:16020", "http://example.com:16020", "http://127.0.0.1:16020/path"} {
		api, err := NewAPI(&fakeExtractor{}, APIConfig{DataDir: t.TempDir(), StudioOrigin: origin})
		if err == nil {
			api.Close()
			t.Fatalf("accepted unsafe origin %q", origin)
		}
	}
}

func submitTask(t *testing.T, baseURL, text string) *Task {
	t.Helper()
	payload, _ := json.Marshal(map[string]string{"text": text})
	response, err := http.Post(baseURL+"/api/parse", "application/json", bytes.NewReader(payload))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusAccepted && response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("status=%d body=%s", response.StatusCode, body)
	}
	var body struct {
		Task *Task `json:"task"`
	}
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil || body.Task == nil {
		t.Fatalf("decode=%v task=%#v", err, body.Task)
	}
	return body.Task
}

func waitTask(t *testing.T, baseURL, id string) *Task {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		response, err := http.Get(baseURL + "/api/tasks/" + id)
		if err != nil {
			t.Fatal(err)
		}
		var body struct {
			Task *Task `json:"task"`
		}
		_ = json.NewDecoder(response.Body).Decode(&body)
		response.Body.Close()
		if body.Task != nil && (body.Task.Status == "completed" || body.Task.Status == "failed") {
			return body.Task
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("task did not complete")
	return nil
}
