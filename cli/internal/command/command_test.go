package command

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"h3studio/cli/internal/config"
	"h3studio/cli/internal/connection"
)

const (
	testAssetID   = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	testJobID     = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	testMediaID   = "cccccccccccccccccccccccccccccccc"
	testProjectID = "dddddddddddddddddddddddddddddddd"
)

func executeTest(t *testing.T, args []string, in string) (int, string, string) {
	t.Helper()
	var out, stderr bytes.Buffer
	code := Execute(context.Background(), args, IOStreams{In: strings.NewReader(in), Out: &out, Err: &stderr})
	return code, out.String(), stderr.String()
}

func TestVideoHelpDocumentsComposeRecoveryAndTrimAlias(t *testing.T) {
	code, out, stderr := executeTest(t, []string{"--server", "http://127.0.0.1:1", "video", "--help"}, "")
	if code != 0 || stderr != "" {
		t.Fatalf("code=%d stderr=%q", code, stderr)
	}
	for _, expected := range []string{"video compose", "video migrate-character", "video trim", "video concat", "motion_context", "project_id", "Turbo4"} {
		if !strings.Contains(out, expected) {
			t.Fatalf("help is missing %q: %s", expected, out)
		}
	}
}

func TestVideoMigrateCharacterPlanUsesStrictVersionedContract(t *testing.T) {
	var calls int
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodPost || r.URL.Path != "/api/video/character-migration/plan" {
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&payload)
		_ = json.NewEncoder(w).Encode(map[string]any{"version": "h3.character-migration/v1", "project": map[string]any{}})
	}))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{
		"--server", server.URL, "video", "migrate-character",
		"--source", "asset:" + testAssetID,
		"--character", "asset:" + testJobID,
		"--source-subject", "the centered dancer in a red dress",
		"--segment-frames", "124", "--overlap-frames", "5", "--steps", "4",
		"--plan-only", "--json",
	}, "")
	if code != 0 || stderr != "" || calls != 1 {
		t.Fatalf("code=%d calls=%d stderr=%q", code, calls, stderr)
	}
	if payload["version"] != "h3.character-migration/v1" || payload["source_asset_id"] != testAssetID {
		t.Fatalf("payload=%v", payload)
	}
	target := payload["targets"].([]any)[0].(map[string]any)
	if target["character_asset_id"] != testJobID || target["source_subject"] != "the centered dancer in a red dress" {
		t.Fatalf("target=%v", target)
	}
}

func TestVideoMigrateCharacterRejectsInvalidSamplingBeforeHTTP(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls++ }))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{
		"--server", server.URL, "video", "migrate-character",
		"--source", "asset:" + testAssetID,
		"--character", "asset:" + testJobID,
		"--source-subject", "the centered dancer", "--steps", "0", "--plan-only",
	}, "")
	if code != 2 || calls != 0 || !strings.Contains(stderr, "--steps must be between 4 and 50") {
		t.Fatalf("code=%d calls=%d stderr=%q", code, calls, stderr)
	}
}

func TestVideoComposeRejectsMissingOutputBeforeNetwork(t *testing.T) {
	code, _, stderr := executeTest(t, []string{"--server", "http://127.0.0.1:1", "video", "compose", "--spec", "-"}, `{}`)
	if code != 2 || !strings.Contains(stderr, "requires --spec PATH|- --to PATH") {
		t.Fatalf("code=%d stderr=%q", code, stderr)
	}
}

type commandFakeProcess struct {
	done    chan struct{}
	once    sync.Once
	stopErr error
	waitErr error
}

func (p *commandFakeProcess) Wait() error { <-p.done; return p.waitErr }
func (p *commandFakeProcess) Exited() bool {
	select {
	case <-p.done:
		return true
	default:
		return false
	}
}
func (p *commandFakeProcess) Stop(context.Context) error {
	p.once.Do(func() { close(p.done) })
	return p.stopErr
}

func commandControlOK(context.Context, string, []string) error { return nil }

func TestJSONStdoutContractAndTrailingGlobalFlag(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
	}))
	defer server.Close()
	code, out, stderr := executeTest(t, []string{"--server", server.URL, "doctor", "--json"}, "")
	if code != 0 {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	var envelope map[string]any
	if err := json.Unmarshal([]byte(out), &envelope); err != nil {
		t.Fatalf("stdout is not JSON: %q", out)
	}
	if envelope["schema_version"] != "h3ctl.output/v1" || envelope["ok"] != true {
		t.Fatalf("unexpected envelope: %v", envelope)
	}
}

func TestTrailingGlobalServerFlag(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
	}))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{"doctor", "--server", server.URL, "--json"}, "")
	if code != 0 || stderr != "" {
		t.Fatalf("code=%d stderr=%q", code, stderr)
	}
}

func TestJSONErrorContract(t *testing.T) {
	code, out, _ := executeTest(t, []string{"generate", "video", "--mode", "bad", "--prompt", "x", "--json"}, "")
	if code != 2 {
		t.Fatalf("code=%d out=%s", code, out)
	}
	var envelope map[string]any
	_ = json.Unmarshal([]byte(out), &envelope)
	failure := envelope["error"].(map[string]any)
	if failure["code"] != "usage" {
		t.Fatalf("unexpected envelope %v", envelope)
	}
}

func TestGenerateImagePayload(t *testing.T) {
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/generate" {
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&payload)
		w.WriteHeader(202)
		_ = json.NewEncoder(w).Encode(map[string]any{"job_id": testJobID})
	}))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "generate", "image", "--prompt", "portrait", "--width", "1024", "--height", "768", "--steps", "12"}, "")
	if code != 0 {
		t.Fatalf("stderr=%s", stderr)
	}
	if payload["output_type"] != "image" || payload["prompt"] != "portrait" {
		t.Fatalf("payload=%v", payload)
	}
	parameters := payload["parameters"].(map[string]any)
	if parameters["width"] != float64(1024) || parameters["steps"] != float64(12) {
		t.Fatalf("parameters=%v", parameters)
	}
	if len(payload["request_id"].(string)) != 32 {
		t.Fatalf("request_id=%v", payload["request_id"])
	}
}

func TestGenerateCommandRejectsEmptyJobReceiptAndKeepsRequestID(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusAccepted)
		_, _ = io.WriteString(w, `{}`)
	}))
	defer server.Close()
	code, out, stderr := executeTest(t, []string{"--server", server.URL, "--json", "--request-id", testMediaID, "generate", "image", "--prompt", "x"}, "")
	if code == 0 || stderr != "" || calls.Load() != 3 {
		t.Fatalf("code=%d calls=%d out=%q stderr=%q", code, calls.Load(), out, stderr)
	}
	var envelope map[string]any
	if err := json.Unmarshal([]byte(out), &envelope); err != nil {
		t.Fatal(err)
	}
	failure := envelope["error"].(map[string]any)
	details := failure["details"].(map[string]any)
	if failure["code"] != "submission_recovery_failed" || details["request_id"] != testMediaID {
		t.Fatalf("envelope=%v", envelope)
	}
}

func TestGenerateFL2VUploadsLocalFrames(t *testing.T) {
	dir := t.TempDir()
	first := filepath.Join(dir, "first.png")
	last := filepath.Join(dir, "last.png")
	_ = os.WriteFile(first, []byte("first"), 0o600)
	_ = os.WriteFile(last, []byte("last"), 0o600)
	var uploads atomic.Int32
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/assets":
			n := uploads.Add(1)
			if err := r.ParseMultipartForm(1024); err != nil {
				t.Fatal(err)
			}
			w.WriteHeader(201)
			_ = json.NewEncoder(w).Encode(map[string]any{"asset_id": fmt.Sprintf("%032x", n)})
		case "/api/generate":
			_ = json.NewDecoder(r.Body).Decode(&payload)
			w.WriteHeader(202)
			_ = json.NewEncoder(w).Encode(map[string]any{"job_id": testJobID})
		default:
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
	}))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "generate", "video", "--mode", "fl2v", "--prompt", "move", "--first-frame", first, "--last-frame", last}, "")
	if code != 0 {
		t.Fatalf("stderr=%s", stderr)
	}
	if uploads.Load() != 2 {
		t.Fatalf("uploads=%d", uploads.Load())
	}
	refs := payload["references"].([]any)
	if len(refs) != 2 || refs[0].(map[string]any)["role"] != "first_frame" || refs[1].(map[string]any)["role"] != "last_frame" {
		t.Fatalf("refs=%v", refs)
	}
}

func TestGenerateVideoRejectsContradictoryModeBeforeNetwork(t *testing.T) {
	code, _, stderr := executeTest(t, []string{"generate", "video", "--mode", "t2v", "--prompt", "x", "--first-frame", "missing.png"}, "")
	if code == 0 || !strings.Contains(stderr, "t2v does not accept") {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
}

func TestMediaEndpointsUsesFirstAndLast(t *testing.T) {
	var positions []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		_ = json.NewDecoder(r.Body).Decode(&payload)
		positions = append(positions, payload["position"].(string))
		w.WriteHeader(201)
		_ = json.NewEncoder(w).Encode(map[string]any{"receipt_id": testMediaID})
	}))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "media", "endpoints", "job:" + testJobID}, "")
	if code != 0 {
		t.Fatal(stderr)
	}
	if strings.Join(positions, ",") != "first,last" {
		t.Fatalf("positions=%v", positions)
	}
}

func TestPrepareReferenceCommandUsesServerDerivationAndReturnsLocator(t *testing.T) {
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/media/derive" {
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&payload)
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"id": testMediaID, "preprocessing": map[string]any{"algorithm_version": "h3-reference-low-token/v1"}})
	}))
	defer server.Close()
	code, out, stderr := executeTest(t, []string{"--server", server.URL, "--json", "media", "prepare-reference", "asset:" + testAssetID, "--preset", "h3-low-token"}, "")
	if code != 0 || stderr != "" {
		t.Fatalf("code=%d out=%s stderr=%s", code, out, stderr)
	}
	if payload["operation"] != "prepare_h3_reference" || payload["preset"] != "h3-low-token" || payload["fps"] != float64(24) {
		t.Fatalf("payload=%v", payload)
	}
	if !strings.Contains(out, "media:"+testMediaID) {
		t.Fatalf("locator missing from output: %s", out)
	}
}

func TestPrepareReferenceCommandStreamsBackgroundProgress(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/media/derive":
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"task_id": testJobID, "status": "queued", "progress": 0})
		case r.Method == http.MethodGet && r.URL.Path == "/api/media-tasks/"+testJobID:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"task_id": testJobID, "status": "completed", "progress": 100,
				"receipt": map[string]any{"id": testMediaID, "receipt_id": testMediaID, "kind": "video"},
			})
		default:
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()
	code, out, stderr := executeTest(t, []string{"--server", server.URL, "--output", "jsonl", "media", "prepare-reference", "asset:" + testAssetID, "--preset", "h3-low-token"}, "")
	if code != 0 || stderr != "" {
		t.Fatalf("code=%d out=%s stderr=%s", code, out, stderr)
	}
	if !strings.Contains(out, `"type":"media_submitted"`) || !strings.Contains(out, `"type":"media_progress"`) || !strings.Contains(out, "media:"+testMediaID) {
		t.Fatalf("background progress or locator missing: %s", out)
	}
}

func TestPrepareReferenceCommandRejectsUnsafeParametersBeforeNetwork(t *testing.T) {
	for _, args := range [][]string{
		{"media", "prepare-reference", "asset:" + testAssetID},
		{"media", "prepare-reference", "asset:" + testAssetID, "--audio", "guess"},
		{"media", "prepare-reference", "asset:" + testAssetID, "--audio", "remove", "--fps", "30"},
	} {
		code, _, _ := executeTest(t, append(args, "--json"), "")
		if code != 2 {
			t.Fatalf("args=%v code=%d", args, code)
		}
	}
}

func TestVoiceConvertWaitsAndDownloadsWithStableAssetResolution(t *testing.T) {
	var submitted map[string]any
	destination := filepath.Join(t.TempDir(), "converted.wav")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/voice/tasks":
			_ = json.NewDecoder(r.Body).Decode(&submitted)
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"task_id": testMediaID, "status": "queued"})
		case r.Method == http.MethodGet && r.URL.Path == "/api/voice/tasks/"+testMediaID:
			_ = json.NewEncoder(w).Encode(map[string]any{"task_id": testMediaID, "status": "completed", "progress": 100})
		case r.Method == http.MethodGet && r.URL.Path == "/api/voice/tasks/"+testMediaID+"/download":
			_, _ = io.WriteString(w, "RIFFvoice")
		default:
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()
	code, out, stderr := executeTest(t, []string{
		"--server", server.URL, "--json", "--request-id", testProjectID,
		"voice", "convert", "asset:" + testAssetID,
		"--reference", "asset:" + testJobID, "--engine", "vevo2",
		"--poll-interval", "1ms", "--to", destination,
	}, "")
	if code != 0 {
		t.Fatalf("code=%d out=%s stderr=%s", code, out, stderr)
	}
	if !strings.Contains(stderr, "completed") {
		t.Fatalf("voice progress missing from stderr: %s", stderr)
	}
	if submitted["engine"] != "vevo2" || submitted["source_asset_id"] != testAssetID || submitted["reference_asset_id"] != testJobID || submitted["request_id"] != testProjectID {
		t.Fatalf("payload=%v", submitted)
	}
	if content, err := os.ReadFile(destination); err != nil || string(content) != "RIFFvoice" {
		t.Fatalf("download=%q err=%v", content, err)
	}
	if !strings.Contains(out, testMediaID) {
		t.Fatalf("task id missing from output: %s", out)
	}
}

func TestVoiceConvertRejectsUnsafeOrContradictoryFlagsBeforeNetwork(t *testing.T) {
	for _, args := range [][]string{
		{"voice", "convert", "asset:" + testAssetID, "--reference", "asset:" + testJobID, "--engine", "unknown"},
		{"voice", "convert", "asset:" + testAssetID, "--engine", "vevo2"},
		{"voice", "convert", "asset:" + testAssetID, "--reference", "asset:" + testJobID, "--engine", "vevo2", "--detach", "--to", "x.wav"},
		{"voice", "convert", "asset:" + testAssetID, "--reference", "asset:" + testJobID, "--engine", "vevo2", "--seed", "42"},
		{"voice", "convert", "asset:" + testAssetID, "--reference", "asset:" + testJobID, "--engine", "yingmusic", "--steps", "9"},
		{"voice", "convert", "asset:" + testAssetID, "--reference", "asset:" + testJobID, "--engine", "yingmusic", "--cfg", "2.1"},
	} {
		code, _, _ := executeTest(t, args, "")
		if code != 2 {
			t.Fatalf("args=%v code=%d", args, code)
		}
	}
}

func TestVoiceConvertYingMusicPassesTuning(t *testing.T) {
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/voice/tasks" {
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&payload)
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(map[string]any{"task_id": testMediaID, "status": "queued"})
	}))
	defer server.Close()
	code, out, stderr := executeTest(t, []string{"--server", server.URL, "voice", "convert", "asset:" + testAssetID,
		"--reference", "asset:" + testJobID, "--engine", "yingmusic", "--steps", "75", "--cfg", "0.9", "--seed", "42", "--detach"}, "")
	if code != 0 || stderr != "" {
		t.Fatalf("code=%d out=%s stderr=%s", code, out, stderr)
	}
	if payload["diffusion_steps"] != float64(75) || payload["inference_cfg_rate"] != 0.9 || payload["seed"] != float64(42) {
		t.Fatalf("payload=%v", payload)
	}
}

func TestResumeCommandWaitsAndDownloadsContinuedResult(t *testing.T) {
	var payload map[string]any
	destination := filepath.Join(t.TempDir(), "continued.mp4")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/jobs/"+testJobID+"/resume":
			_ = json.NewDecoder(r.Body).Decode(&payload)
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"job_id": testMediaID, "steps_before": 7, "steps_after": 10})
		case r.Method == http.MethodGet && r.URL.Path == "/api/status":
			_ = json.NewEncoder(w).Encode(map[string]any{"id": testMediaID, "status": "completed", "output_type": "video"})
		case r.Method == http.MethodGet && r.URL.Path == "/api/download":
			_, _ = io.WriteString(w, "continued-video")
		default:
			t.Fatalf("unexpected %s %s", r.Method, r.URL.String())
		}
	}))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "job", "resume", testJobID, "--additional-steps", "3", "--wait", "--poll-interval", "1ms", "--download", destination}, "")
	if code != 0 {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	if payload["additional_steps"] != float64(3) || len(payload["request_id"].(string)) != 32 {
		t.Fatalf("payload=%v", payload)
	}
	content, err := os.ReadFile(destination)
	if err != nil || string(content) != "continued-video" {
		t.Fatalf("download=%q err=%v", content, err)
	}
}

func TestResumeCommandRejectsNonPositiveSteps(t *testing.T) {
	for _, value := range []string{"0", "-1"} {
		code, _, _ := executeTest(t, []string{"job", "resume", testJobID, "--additional-steps", value, "--json"}, "")
		if code != 2 {
			t.Fatalf("value=%s code=%d", value, code)
		}
	}
}

func TestHelpIncludesAgentContracts(t *testing.T) {
	code, out, _ := executeTest(t, []string{"generate", "video", "--help"}, "")
	if code != 0 || !strings.Contains(out, "--source-video") || !strings.Contains(out, "Ctrl-C") {
		t.Fatalf("help=%q", out)
	}
}

func TestRootHelp(t *testing.T) {
	code, out, stderr := executeTest(t, []string{"--help"}, "")
	if code != 0 || stderr != "" || !strings.Contains(out, "operation") || !strings.Contains(out, "douyin") {
		t.Fatalf("code=%d out=%q stderr=%q", code, out, stderr)
	}
	for _, forbidden := range []string{"api-key", "API key", "H3_STUDIO_API_KEY"} {
		if strings.Contains(out, forbidden) || strings.Contains(ContextHelp, forbidden) {
			t.Fatalf("legacy credential UX remains in help: %q", forbidden)
		}
	}
}

func TestDouyinCommandsAreLocalAndRejectUnsafeLinksBeforeProcess(t *testing.T) {
	var starts atomic.Int32
	options := connection.Options{Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) {
		starts.Add(1)
		return &commandFakeProcess{done: make(chan struct{})}, nil
	}}
	for _, args := range [][]string{{"douyin", "--help"}, {"douyin", "serve", "--help"}} {
		var out, stderr bytes.Buffer
		if code := executeWithConnectionOptions(context.Background(), args, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options); code != 0 || !strings.Contains(out.String(), "Swagger") {
			t.Fatalf("args=%v code=%d out=%q stderr=%q", args, code, out.String(), stderr.String())
		}
	}
	executable, _ := os.Executable()
	var out, stderr bytes.Buffer
	code := executeWithConnectionOptions(context.Background(), []string{"douyin", "parse", "https://example.com/private", "--yt-dlp", executable, "--json"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options)
	if code != 2 || !strings.Contains(out.String(), `"code":"invalid_link"`) {
		t.Fatalf("code=%d out=%q stderr=%q", code, out.String(), stderr.String())
	}
	if starts.Load() != 0 {
		t.Fatalf("Douyin command started SSH %d times", starts.Load())
	}
}

func TestDouyinOutputTemplateIsLocalAndNonDestructive(t *testing.T) {
	directory := t.TempDir()
	template, err := douyinOutputTemplate(directory, false)
	if err != nil || !strings.HasSuffix(template, "%(uploader).80B-%(id)s.%(ext)s") {
		t.Fatalf("template=%q err=%v", template, err)
	}
	file := filepath.Join(directory, "clip.mp4")
	if err := os.WriteFile(file, []byte("existing"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := douyinOutputTemplate(file, false); err == nil {
		t.Fatal("existing exact destination was accepted without --force")
	}
	if value, err := douyinOutputTemplate(file, true); err != nil || value != file {
		t.Fatalf("force value=%q err=%v", value, err)
	}
}

func TestContextAddKeepsCommandServerFlag(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	code, out, stderr := executeTest(t, []string{"context", "add", "dev", "--server", "https://dev.example", "--json"}, "")
	if code != 0 || stderr != "" {
		t.Fatalf("code=%d out=%q stderr=%q", code, out, stderr)
	}
	raw, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(raw, []byte("https://dev.example")) || bytes.Contains(raw, []byte("api_key")) {
		t.Fatalf("config=%s", raw)
	}
}

func TestContextSSHAddUpdateAndCommandsDoNotStartTunnel(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	var starts atomic.Int32
	options := connection.Options{Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) {
		starts.Add(1)
		return &commandFakeProcess{done: make(chan struct{})}, nil
	}}
	for _, args := range [][]string{
		{"context", "add", "dev", "--ssh-target", "h3-dev", "--ssh-port", "2222", "--json"},
		{"context", "update", "dev", "--ssh-target", "h3-dev-2", "--remote-api-port", "6030", "--json"},
		{"context", "list", "--json"}, {"context", "show", "dev", "--json"}, {"context", "use", "dev", "--json"},
	} {
		var out, stderr bytes.Buffer
		if code := executeWithConnectionOptions(context.Background(), args, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options); code != 0 {
			t.Fatalf("args=%v code=%d out=%s err=%s", args, code, out.String(), stderr.String())
		}
	}
	if starts.Load() != 0 {
		t.Fatalf("configuration commands started SSH %d times", starts.Load())
	}
	value, _ := (config.Store{Path: configPath}).Load()
	if value.Contexts["dev"].SSHTarget != "h3-dev-2" || value.Contexts["dev"].RemoteAPIPort != 6030 || value.Contexts["dev"].SSHPort != 2222 {
		t.Fatalf("unexpected update: %#v", value.Contexts["dev"])
	}
	code, _, _ := executeTest(t, []string{"context", "add", "bad", "--server", "http://localhost:6020", "--ssh-target", "dev", "--json"}, "")
	if code != 2 {
		t.Fatalf("mutually exclusive context accepted, code=%d", code)
	}
	for _, args := range [][]string{{"context", "add", "bad-port", "--ssh-target", "dev", "--ssh-port", "0", "--json"}, {"context", "update", "dev", "--remote-api-port", "0", "--json"}} {
		code, _, _ := executeTest(t, args, "")
		if code != 2 {
			t.Fatalf("invalid port accepted: %v code=%d", args, code)
		}
	}
	code, _, _ = executeTest(t, []string{"context", "update", "dev", "--clear-ssh-port", "--json"}, "")
	if code != 0 {
		t.Fatalf("clear ssh port failed: code=%d", code)
	}
	value, _ = (config.Store{Path: configPath}).Load()
	if value.Contexts["dev"].SSHPort != 0 {
		t.Fatalf("SSH port was not cleared: %#v", value.Contexts["dev"])
	}
	for _, args := range [][]string{{"context", "update", "dev", "--json"}, {"context", "update", "dev", "--ssh-port", "12900", "--clear-ssh-port", "--json"}, {"context", "update", "dev", "--clear-ssh-port=false", "--json"}} {
		code, _, _ := executeTest(t, args, "")
		if code != 2 {
			t.Fatalf("invalid update accepted: %v code=%d", args, code)
		}
	}
}

func TestContextAddConflictPreservesExistingValue(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	store := config.Store{Path: configPath}
	original := config.Context{Server: "https://original.example"}
	if err := store.Save(config.File{Current: "dev", Contexts: map[string]config.Context{"dev": original}}); err != nil {
		t.Fatal(err)
	}
	code, out, _ := executeTest(t, []string{"context", "add", "dev", "--server", "https://replacement.example", "--json"}, "")
	if code == 0 || !strings.Contains(out, `"code":"conflict"`) {
		t.Fatalf("duplicate add did not conflict: code=%d out=%s", code, out)
	}
	value, _ := store.Load()
	if value.Contexts["dev"] != original {
		t.Fatalf("duplicate add changed original: %#v", value.Contexts["dev"])
	}
}

func TestContextTestStartsAndClosesSSHWithJSONStdout(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
	}))
	defer server.Close()
	portText := strings.TrimPrefix(server.URL, "http://127.0.0.1:")
	port, _ := strconv.Atoi(portText)
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	store := config.Store{Path: configPath}
	if err := store.Save(config.File{Current: "dev", Contexts: map[string]config.Context{"dev": {SSHTarget: "alias"}}}); err != nil {
		t.Fatal(err)
	}
	process := &commandFakeProcess{done: make(chan struct{})}
	var args []string
	options := connection.Options{
		StartupTimeout: time.Second,
		AllocatePort:   func() (int, error) { return port, nil },
		RunControl:     commandControlOK,
		Probe:          func(context.Context, string) error { return nil },
		Start: func(_ string, value []string, _ io.Reader, _ io.Writer) (connection.Process, error) {
			args = append([]string{}, value...)
			return process, nil
		},
	}
	var out, stderr bytes.Buffer
	code := executeWithConnectionOptions(context.Background(), []string{"context", "test", "dev", "--non-interactive", "--json"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options)
	if code != 0 || stderr.Len() != 0 {
		t.Fatalf("code=%d out=%s stderr=%s", code, out.String(), stderr.String())
	}
	var envelope map[string]any
	if json.Unmarshal(out.Bytes(), &envelope) != nil || envelope["ok"] != true {
		t.Fatalf("stdout is not clean JSON: %q", out.String())
	}
	if !strings.Contains(strings.Join(args, " "), "BatchMode=yes") {
		t.Fatalf("missing BatchMode: %v", args)
	}
	select {
	case <-process.done:
	default:
		t.Fatal("context test did not close SSH")
	}
}

func TestSSHFailureKeepsJSONStdoutCleanAndDiagnosticsOnStderr(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	if err := (config.Store{Path: configPath}).Save(config.File{Current: "dev", Contexts: map[string]config.Context{"dev": {SSHTarget: "alias"}}}); err != nil {
		t.Fatal(err)
	}
	process := &commandFakeProcess{done: make(chan struct{})}
	process.once.Do(func() { close(process.done) })
	options := connection.Options{
		StartupTimeout: 100 * time.Millisecond,
		CleanupTimeout: 20 * time.Millisecond,
		AllocatePort:   func() (int, error) { return 41235, nil },
		RunControl:     commandControlOK,
		Probe:          func(context.Context, string) error { return fmt.Errorf("not ready") },
		Start: func(_ string, _ []string, _ io.Reader, stderr io.Writer) (connection.Process, error) {
			_, _ = io.WriteString(stderr, "fake ssh diagnostic\n")
			return process, nil
		},
	}
	var out, stderr bytes.Buffer
	code := executeWithConnectionOptions(context.Background(), []string{"doctor", "--json"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options)
	if code == 0 || !strings.Contains(stderr.String(), "fake ssh diagnostic") {
		t.Fatalf("code=%d stderr=%q", code, stderr.String())
	}
	var envelope map[string]any
	if err := json.Unmarshal(out.Bytes(), &envelope); err != nil || envelope["ok"] != false {
		t.Fatalf("stdout contaminated: %q err=%v", out.String(), err)
	}
}

func TestExecuteKeepsMainSSHTunnelUntilCommandReturnsThenCloses(t *testing.T) {
	process := &commandFakeProcess{done: make(chan struct{})}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		select {
		case <-process.done:
			t.Error("tunnel closed while API command was running")
		default:
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "profiles": []any{}})
	}))
	defer server.Close()
	port, _ := strconv.Atoi(strings.TrimPrefix(server.URL, "http://127.0.0.1:"))
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	if err := (config.Store{Path: configPath}).Save(config.File{Current: "dev", Contexts: map[string]config.Context{"dev": {SSHTarget: "alias"}}}); err != nil {
		t.Fatal(err)
	}
	options := connection.Options{
		AllocatePort: func() (int, error) { return port, nil },
		RunControl:   commandControlOK,
		Probe:        func(context.Context, string) error { return nil },
		Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) {
			return process, nil
		},
	}
	var out, stderr bytes.Buffer
	if code := executeWithConnectionOptions(context.Background(), []string{"doctor", "--json"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options); code != 0 {
		t.Fatalf("code=%d out=%s stderr=%s", code, out.String(), stderr.String())
	}
	select {
	case <-process.done:
	default:
		t.Fatal("Execute did not close main SSH session")
	}
}

func TestExecuteSucceedsWhenAcceptedControlExitReturnsMasterStatus255(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "profiles": []any{}})
	}))
	defer server.Close()
	port, _ := strconv.Atoi(strings.TrimPrefix(server.URL, "http://127.0.0.1:"))
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	if err := (config.Store{Path: configPath}).Save(config.File{Current: "dev", Contexts: map[string]config.Context{"dev": {SSHTarget: "alias"}}}); err != nil {
		t.Fatal(err)
	}
	process := &commandFakeProcess{done: make(chan struct{}), waitErr: errors.New("exit status 255")}
	options := connection.Options{
		AllocatePort: func() (int, error) { return port, nil },
		Probe:        func(context.Context, string) error { return nil },
		Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) {
			return process, nil
		},
		RunControl: func(_ context.Context, _ string, args []string) error {
			for index, value := range args {
				if value == "-O" && index+1 < len(args) && args[index+1] == "exit" {
					process.once.Do(func() { close(process.done) })
				}
			}
			return nil
		},
	}
	var out, stderr bytes.Buffer
	code := executeWithConnectionOptions(context.Background(), []string{"doctor", "--json"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options)
	if code != 0 || !strings.Contains(out.String(), `"ok":true`) {
		t.Fatalf("code=%d out=%s stderr=%s", code, out.String(), stderr.String())
	}
}

func TestExecuteReportsCleanupFailureBeforeSuccessOutput(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "profiles": []any{}})
	}))
	defer server.Close()
	port, _ := strconv.Atoi(strings.TrimPrefix(server.URL, "http://127.0.0.1:"))
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	if err := (config.Store{Path: configPath}).Save(config.File{Current: "dev", Contexts: map[string]config.Context{"dev": {SSHTarget: "alias"}}}); err != nil {
		t.Fatal(err)
	}
	process := &commandFakeProcess{done: make(chan struct{}), stopErr: errors.New("cannot stop master")}
	options := connection.Options{
		AllocatePort: func() (int, error) { return port, nil }, RunControl: commandControlOK,
		Probe: func(context.Context, string) error { return nil },
		Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) { return process, nil },
	}
	var out, stderr bytes.Buffer
	code := executeWithConnectionOptions(context.Background(), []string{"doctor", "--json"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options)
	if code == 0 || strings.Contains(out.String(), `"ok":true`) || !strings.Contains(out.String(), `"code":"ssh_cleanup_failed"`) {
		t.Fatalf("cleanup failure was hidden: code=%d out=%s stderr=%s", code, out.String(), stderr.String())
	}
}

func TestExecutePreservesBusinessErrorAndAddsCleanupDetails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, `{"error":{"code":"server_busy","message":"busy"}}`)
	}))
	defer server.Close()
	port, _ := strconv.Atoi(strings.TrimPrefix(server.URL, "http://127.0.0.1:"))
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	_ = (config.Store{Path: configPath}).Save(config.File{Current: "dev", Contexts: map[string]config.Context{"dev": {SSHTarget: "alias"}}})
	process := &commandFakeProcess{done: make(chan struct{}), stopErr: errors.New("cannot stop master")}
	options := connection.Options{
		AllocatePort: func() (int, error) { return port, nil }, RunControl: commandControlOK,
		Probe: func(context.Context, string) error { return nil },
		Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) { return process, nil },
	}
	var out, stderr bytes.Buffer
	code := executeWithConnectionOptions(context.Background(), []string{"doctor", "--json"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options)
	if code == 0 || !strings.Contains(out.String(), `"code":"server_busy"`) || !strings.Contains(out.String(), `"cleanup_error"`) {
		t.Fatalf("primary error was not preserved: code=%d out=%s stderr=%s", code, out.String(), stderr.String())
	}
}

func TestLocalCommandsNeverStartOfflineCurrentSSH(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	if err := (config.Store{Path: configPath}).Save(config.File{Current: "offline", Contexts: map[string]config.Context{"offline": {SSHTarget: "offline-alias"}}}); err != nil {
		t.Fatal(err)
	}
	var starts atomic.Int32
	options := connection.Options{Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) {
		starts.Add(1)
		return nil, errors.New("must not start")
	}}
	tests := []struct {
		args []string
		code int
	}{
		{args: []string{"version", "--help"}},
		{args: []string{"context", "--help"}},
		{args: []string{"doctor", "--help"}},
		{args: []string{"capability", "--help"}},
		{args: []string{"profile", "--help"}},
		{args: []string{"asset", "--help"}},
		{args: []string{"generate", "--help"}},
		{args: []string{"job", "wait", "--help"}},
		{args: []string{"media", "--help"}},
		{args: []string{"voice", "--help"}},
		{args: []string{"project", "--help"}},
		{args: []string{"operation", "--help"}},
		{args: []string{"unknown"}, code: 2},
		{args: []string{"job", "unknown"}, code: 2},
		{args: []string{"capability", "unknown", "--json"}, code: 2},
		{args: []string{"capability", "show", "invalid", "--json"}, code: 2},
		{args: []string{"profile", "unknown", "--json"}, code: 2},
		{args: []string{"profile", "show", "--json"}, code: 2},
		{args: []string{"workflow", "--help"}},
		{args: []string{"workflow", "run"}, code: 8},
		{args: []string{"completion", "--help"}},
		{args: []string{"completion", "bash"}, code: 8},
		{args: []string{"operation", "list", "--json"}},
		{args: []string{"operation", "schema", "job.get", "--json"}},
	}
	for _, test := range tests {
		var out, stderr bytes.Buffer
		code := executeWithConnectionOptions(context.Background(), test.args, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options)
		if code != test.code {
			t.Fatalf("args=%v code=%d want=%d out=%s stderr=%s", test.args, code, test.code, out.String(), stderr.String())
		}
	}
	if starts.Load() != 0 {
		t.Fatalf("purely local commands started SSH %d times", starts.Load())
	}
}

func TestConnectionDecisionCoversEveryRemoteCommandAction(t *testing.T) {
	runner := &Runner{}
	actions := map[string][]string{
		"capability": {"list", "show"},
		"profile":    {"list", "show"},
		"asset":      {"upload", "download", "list", "get", "copy", "update", "pin", "delete"},
		"generate":   {"image", "video"},
		"job":        {"list", "get", "wait", "resume", "cancel", "download", "save", "workflow", "delete"},
		"media":      {"frame", "endpoints", "trim", "extract-audio", "remove-audio", "mux-audio", "prepare-reference", "list", "get", "download", "save", "delete"},
		"voice":      {"convert", "status", "wait", "cancel", "delete", "download", "capabilities"},
		"project":    {"list", "create", "apply", "get", "delete", "run", "wait", "stop", "rerun", "merge", "download"},
		"video":      {"compose", "migrate-character", "trim", "concat"},
	}
	if len(networkCommandActions) != len(actions) {
		t.Fatalf("network policy top-level drift: %#v", networkCommandActions)
	}
	for command, values := range actions {
		if len(networkCommandActions[command]) != len(values) {
			t.Fatalf("network policy drift for %s: %#v", command, networkCommandActions[command])
		}
		for _, action := range values {
			if command == "asset" && action == "copy" {
				if runner.commandNeedsConnection(command, []string{action}) {
					t.Error("asset copy must own only its source/destination sessions")
				}
				continue
			}
			args := []string{action}
			if command == "capability" && action == "show" {
				args = append(args, "video")
			}
			if command == "profile" && action == "show" {
				args = append(args, "profile-id")
			}
			if !runner.commandNeedsConnection(command, args) {
				t.Errorf("%s %s was incorrectly classified as local", command, action)
			}
		}
	}
}

func TestAssetCopyOpensExactlyTwoSSHSession(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/content"):
			_, _ = io.WriteString(w, "video")
		case r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(map[string]any{"id": testAssetID, "filename": "clip.mp4", "kind": "video"})
		case r.Method == http.MethodPost && r.URL.Path == "/api/assets":
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"asset_id": testMediaID})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	port, _ := strconv.Atoi(strings.TrimPrefix(server.URL, "http://127.0.0.1:"))
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	if err := (config.Store{Path: configPath}).Save(config.File{Current: "source", Contexts: map[string]config.Context{
		"source": {SSHTarget: "source-alias"}, "dest": {SSHTarget: "dest-alias"},
	}}); err != nil {
		t.Fatal(err)
	}
	var starts atomic.Int32
	var processes []*commandFakeProcess
	options := connection.Options{
		AllocatePort: func() (int, error) { return port, nil },
		RunControl:   commandControlOK,
		Probe:        func(context.Context, string) error { return nil },
		Start: func(_ string, _ []string, _ io.Reader, _ io.Writer) (connection.Process, error) {
			starts.Add(1)
			process := &commandFakeProcess{done: make(chan struct{})}
			processes = append(processes, process)
			return process, nil
		},
	}
	var out, stderr bytes.Buffer
	code := executeWithConnectionOptions(context.Background(), []string{"asset", "copy", "h3://source/assets/" + testAssetID, "--to-context", "dest", "--json"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options)
	if code != 0 || starts.Load() != 2 {
		t.Fatalf("code=%d starts=%d out=%s stderr=%s", code, starts.Load(), out.String(), stderr.String())
	}
	for _, process := range processes {
		select {
		case <-process.done:
		default:
			t.Fatal("copy did not close a tunnel")
		}
	}
}

func TestOperationAssetCopyUsesOnlySourceAndDestinationSessions(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/content"):
			_, _ = io.WriteString(w, "video")
		case r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(map[string]any{"id": testAssetID, "filename": "clip.mp4", "kind": "video"})
		case r.Method == http.MethodPost && r.URL.Path == "/api/assets":
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"asset_id": testMediaID})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	port, _ := strconv.Atoi(strings.TrimPrefix(server.URL, "http://127.0.0.1:"))
	tests := []struct {
		name           string
		source, dest   config.Context
		expectedStarts int32
	}{
		{name: "direct-direct", source: config.Context{Server: server.URL}, dest: config.Context{Server: server.URL}},
		{name: "direct-ssh", source: config.Context{Server: server.URL}, dest: config.Context{SSHTarget: "dest"}, expectedStarts: 1},
		{name: "ssh-direct", source: config.Context{SSHTarget: "source"}, dest: config.Context{Server: server.URL}, expectedStarts: 1},
		{name: "ssh-ssh", source: config.Context{SSHTarget: "source"}, dest: config.Context{SSHTarget: "dest"}, expectedStarts: 2},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			configPath := filepath.Join(t.TempDir(), "config.json")
			t.Setenv("H3CTL_CONFIG", configPath)
			if err := (config.Store{Path: configPath}).Save(config.File{Current: "unused", Contexts: map[string]config.Context{
				"unused": {SSHTarget: "must-not-start"}, "source": test.source, "dest": test.dest,
			}}); err != nil {
				t.Fatal(err)
			}
			var starts atomic.Int32
			var processes []*commandFakeProcess
			options := connection.Options{
				AllocatePort: func() (int, error) { return port, nil },
				RunControl:   commandControlOK,
				Probe:        func(context.Context, string) error { return nil },
				Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) {
					starts.Add(1)
					process := &commandFakeProcess{done: make(chan struct{})}
					processes = append(processes, process)
					return process, nil
				},
			}
			input := fmt.Sprintf(`{"source":"h3://source/assets/%s","to_context":"dest"}`, testAssetID)
			var out, stderr bytes.Buffer
			code := executeWithConnectionOptions(context.Background(), []string{"operation", "run", "asset.copy", "--input", "-", "--json"}, IOStreams{In: strings.NewReader(input), Out: &out, Err: &stderr}, options)
			if code != 0 || starts.Load() != test.expectedStarts {
				t.Fatalf("code=%d starts=%d want=%d out=%s stderr=%s", code, starts.Load(), test.expectedStarts, out.String(), stderr.String())
			}
			for _, process := range processes {
				select {
				case <-process.done:
				default:
					t.Fatal("operation copy leaked SSH process")
				}
			}
		})
	}
}

func TestOperationAssetCopyDestinationStartFailureReclaimsSource(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/content") {
			_, _ = io.WriteString(w, "video")
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": testAssetID, "filename": "clip.mp4", "kind": "video"})
	}))
	defer server.Close()
	port, _ := strconv.Atoi(strings.TrimPrefix(server.URL, "http://127.0.0.1:"))
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	if err := (config.Store{Path: configPath}).Save(config.File{Current: "unused", Contexts: map[string]config.Context{
		"unused": {SSHTarget: "must-not-start"}, "source": {SSHTarget: "source"}, "dest": {SSHTarget: "dest"},
	}}); err != nil {
		t.Fatal(err)
	}
	sourceProcess := &commandFakeProcess{done: make(chan struct{}), stopErr: errors.New("source cleanup failed")}
	var starts atomic.Int32
	options := connection.Options{
		AllocatePort: func() (int, error) { return port, nil },
		RunControl:   commandControlOK,
		Probe:        func(context.Context, string) error { return nil },
		Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) {
			if starts.Add(1) == 1 {
				return sourceProcess, nil
			}
			return nil, errors.New("destination ssh failed")
		},
	}
	input := fmt.Sprintf(`{"source":"h3://source/assets/%s","to_context":"dest"}`, testAssetID)
	var out, stderr bytes.Buffer
	code := executeWithConnectionOptions(context.Background(), []string{"operation", "run", "asset.copy", "--input", "-", "--json"}, IOStreams{In: strings.NewReader(input), Out: &out, Err: &stderr}, options)
	if code == 0 || starts.Load() != 2 || !strings.Contains(out.String(), `"cleanup_error"`) {
		t.Fatalf("code=%d starts=%d out=%s stderr=%s", code, starts.Load(), out.String(), stderr.String())
	}
	select {
	case <-sourceProcess.done:
	default:
		t.Fatal("source tunnel was not reclaimed after destination startup failure")
	}
}

func TestAssetCopySuccessfulBusinessFailsWhenDestinationCleanupFails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/content"):
			_, _ = io.WriteString(w, "video")
		case r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(map[string]any{"id": testAssetID, "filename": "clip.mp4", "kind": "video"})
		case r.Method == http.MethodPost:
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"asset_id": testMediaID})
		}
	}))
	defer server.Close()
	port, _ := strconv.Atoi(strings.TrimPrefix(server.URL, "http://127.0.0.1:"))
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	_ = (config.Store{Path: configPath}).Save(config.File{Contexts: map[string]config.Context{
		"source": {Server: server.URL}, "dest": {SSHTarget: "dest"},
	}})
	process := &commandFakeProcess{done: make(chan struct{}), stopErr: errors.New("destination cleanup failed")}
	options := connection.Options{
		AllocatePort: func() (int, error) { return port, nil }, RunControl: commandControlOK,
		Probe: func(context.Context, string) error { return nil },
		Start: func(string, []string, io.Reader, io.Writer) (connection.Process, error) { return process, nil },
	}
	var out, stderr bytes.Buffer
	code := executeWithConnectionOptions(context.Background(), []string{"asset", "copy", "h3://source/assets/" + testAssetID, "--to-context", "dest", "--json"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr}, options)
	if code == 0 || strings.Contains(out.String(), `"ok":true`) || !strings.Contains(out.String(), `"code":"ssh_cleanup_failed"`) {
		t.Fatalf("cleanup failure was hidden: code=%d out=%s stderr=%s", code, out.String(), stderr.String())
	}
}

func TestLegacyInvalidContextListAndShowNeverLeakStoredServer(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	secret := "legacy-secret-value"
	raw := fmt.Sprintf(`{"current":"legacy","contexts":{"legacy":{"server":"https://user:%s@example.test","api_key_env":"LEGACY_KEY"}}}`, secret)
	if err := os.WriteFile(configPath, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{{"context", "list", "--json"}, {"context", "show", "legacy", "--json"}} {
		code, out, stderr := executeTest(t, args, "")
		combined := out + stderr
		if code != 0 || strings.Contains(combined, secret) || strings.Contains(combined, "user:") || strings.Contains(combined, "example.test") {
			t.Fatalf("args=%v code=%d out=%q stderr=%q", args, code, out, stderr)
		}
		if !strings.Contains(out, `"status":"invalid"`) || !strings.Contains(out, `"name":"legacy"`) {
			t.Fatalf("missing safe invalid view: %s", out)
		}
	}
}

func TestLegacyCredentialMetadataIsNeverShownAndNextWriteRemovesIt(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	legacy := "LEGACY_SECRET_ENV_NAME"
	raw := fmt.Sprintf(`{"current":"dev","contexts":{"dev":{"server":"https://example.test","api_key_env":%q}}}`, legacy)
	if err := os.WriteFile(configPath, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{{"context", "show", "dev", "--json"}, {"context", "list", "--json"}} {
		code, out, stderr := executeTest(t, args, "")
		if code != 0 || strings.Contains(out+stderr, legacy) || strings.Contains(out+stderr, "api_key") {
			t.Fatalf("legacy metadata leaked: args=%v code=%d out=%s stderr=%s", args, code, out, stderr)
		}
	}
	if code, out, stderr := executeTest(t, []string{"context", "use", "dev", "--json"}, ""); code != 0 {
		t.Fatalf("write failed: code=%d out=%s stderr=%s", code, out, stderr)
	}
	saved, _ := os.ReadFile(configPath)
	if bytes.Contains(saved, []byte("api_key")) || bytes.Contains(saved, []byte(legacy)) {
		t.Fatalf("legacy metadata survived write: %s", saved)
	}
}

func TestSpecFromStdin(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		if !bytes.Contains(raw, []byte(`"output_type":"image"`)) {
			t.Errorf("body=%s", raw)
		}
		w.WriteHeader(202)
		_, _ = w.Write([]byte(`{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`))
	}))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "generate", "image", "--spec", "-"}, `{"output_type":"image","prompt":"stdin"}`)
	if code != 0 {
		t.Fatal(stderr)
	}
}

func TestDocumentedInterspersedCommands(t *testing.T) {
	assetFile := filepath.Join(t.TempDir(), "go.mod")
	_ = os.WriteFile(assetFile, []byte("module test"), 0o600)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/assets" && r.Method == http.MethodPost:
			_ = r.ParseMultipartForm(1 << 20)
			w.WriteHeader(201)
			_, _ = io.WriteString(w, `{"asset_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`)
		case r.URL.Path == "/api/assets/"+testAssetID+"/content":
			_, _ = io.WriteString(w, "asset")
		case r.URL.Path == "/api/status":
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "completed", "job_id": testJobID})
		case r.URL.Path == "/api/media/derive":
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			w.WriteHeader(201)
			_ = json.NewEncoder(w).Encode(map[string]any{"receipt_id": testMediaID, "parameters": body})
		case r.URL.Path == "/api/video-projects/"+testProjectID && r.Method == http.MethodPut:
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			body["id"] = testProjectID
			_ = json.NewEncoder(w).Encode(body)
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL.String())
			w.WriteHeader(404)
			_, _ = io.WriteString(w, `{"error":{"code":"not_found","message":"missing"}}`)
		}
	}))
	defer server.Close()
	prefix := []string{"--server", server.URL}
	download := filepath.Join(t.TempDir(), "a.bin")
	tests := []struct {
		name  string
		args  []string
		input string
	}{{"asset upload", []string{"asset", "upload", assetFile, "--kind", "video"}, ""}, {"asset download", []string{"asset", "download", "asset:" + testAssetID, "--to", download}, ""}, {"job wait", []string{"job", "wait", testJobID, "--timeout", "1s", "--poll-interval", "1ms"}, ""}, {"media frame", []string{"media", "frame", "job:" + testJobID + "#0", "--position", "first"}, ""}, {"media trim", []string{"media", "trim", "asset:" + testAssetID, "--start", "0", "--end", "1"}, ""}, {"project apply", []string{"project", "apply", testProjectID, "--spec", "-"}, `{"title":"x"}`}, {"operation run", []string{"operation", "run", "media.frame", "--input", "-"}, fmt.Sprintf(`{"source":"job:%s#0","position":"first"}`, testJobID)}}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			args := append(append([]string{}, prefix...), test.args...)
			code, _, stderr := executeTest(t, args, test.input)
			if code != 0 {
				t.Fatalf("code=%d stderr=%s", code, stderr)
			}
		})
	}
}

func TestInterspersedFlagsRemainStrict(t *testing.T) {
	for _, args := range [][]string{{"asset", "download", "asset:a1", "--wat"}, {"job", "wait", "j", "--timeout", "1s", "--timeout", "2s"}, {"media", "frame", "job:j", "--position"}} {
		code, _, stderr := executeTest(t, append([]string{"--json"}, args...), "")
		if code != 2 || stderr != "" {
			t.Fatalf("args=%v code=%d stderr=%q", args, code, stderr)
		}
	}
}

func TestGlobalExtractionRespectsDoubleDashAndJSONLErrorPreparse(t *testing.T) {
	code, out, stderr := executeTest(t, []string{"version", "--", "--output=json"}, "")
	if code != 2 || out != "" || !strings.Contains(stderr, "does not accept") {
		t.Fatalf("double dash was ignored: code=%d out=%q stderr=%q", code, out, stderr)
	}
	for _, args := range [][]string{
		{"generate", "video", "--mode", "t2v", "--prompt", "x", "--duration", "bad", "--output=jsonl"},
		{"--output", "jsonl", "generate", "video", "--mode", "t2v", "--prompt", "x", "--duration", "bad"},
	} {
		code, out, stderr = executeTest(t, args, "")
		if code != 2 || stderr != "" {
			t.Fatalf("args=%v code=%d out=%q stderr=%q", args, code, out, stderr)
		}
		var envelope map[string]any
		if json.Unmarshal([]byte(out), &envelope) != nil || envelope["ok"] != false {
			t.Fatalf("not protocol JSONL: %q", out)
		}
	}
}

func TestHelpTokensUsedAsValuesOrIDsExecuteNormally(t *testing.T) {
	t.Setenv("H3CTL_CONFIG", filepath.Join(t.TempDir(), "config.json"))
	var paths []string
	var generated map[string]any
	var updated map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		if r.URL.Path == "/api/generate" {
			_ = json.NewDecoder(r.Body).Decode(&generated)
			w.WriteHeader(http.StatusAccepted)
			_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
			return
		}
		if r.Method == http.MethodPatch {
			_ = json.NewDecoder(r.Body).Decode(&updated)
			_ = json.NewEncoder(w).Encode(map[string]any{"id": testAssetID})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": testAssetID})
	}))
	defer server.Close()
	if code, out, stderr := executeTest(t, []string{"--server", server.URL, "asset", "get", "help"}, ""); code != 2 || strings.Contains(out, "Usage:") || strings.Contains(stderr, "Usage:") {
		t.Fatalf("id help triggered help instead of ID validation: code=%d out=%q stderr=%q", code, out, stderr)
	}
	if code, out, stderr := executeTest(t, []string{"--server", server.URL, "generate", "image", "--prompt", "help"}, ""); code != 0 || strings.Contains(out, "Usage:") || stderr != "" {
		t.Fatalf("prompt help triggered help: code=%d out=%q stderr=%q", code, out, stderr)
	}
	if code, out, stderr := executeTest(t, []string{"--server", server.URL, "asset", "update", testAssetID, "--name", "help"}, ""); code != 0 || strings.Contains(out, "Usage:") || stderr != "" {
		t.Fatalf("name help triggered help: code=%d out=%q stderr=%q", code, out, stderr)
	}
	if generated["prompt"] != "help" || updated["display_name"] != "help" || len(paths) != 2 {
		t.Fatalf("paths=%v generated=%v updated=%v", paths, generated, updated)
	}
	if code, out, stderr := executeTest(t, []string{"--server", server.URL, "generate", "image", "--prompt", "--help"}, ""); code != 0 || strings.Contains(out, "Usage:") || stderr != "" || generated["prompt"] != "--help" {
		t.Fatalf("flag value --help triggered help: code=%d out=%q stderr=%q payload=%v", code, out, stderr, generated)
	}
	if code, out, stderr := executeTest(t, []string{"context", "add", "dev", "--server", "--help"}, ""); code != 2 || strings.Contains(out, "Usage:") || strings.Contains(stderr, "Usage:") {
		t.Fatalf("server value --help triggered help: code=%d out=%q stderr=%q", code, out, stderr)
	}
	code, out, _ := executeTest(t, []string{"generate", "image", "--help", "--json"}, "")
	if code != 0 || !strings.HasPrefix(out, "Usage:") {
		t.Fatalf("JSON help must remain documented plaintext: code=%d out=%q", code, out)
	}
}

func TestGlobalParseErrorsAreStructuredAndNeverPanic(t *testing.T) {
	for _, args := range [][]string{{"--output", "invalid", "doctor"}, {"--server"}, {"--control-timeout", "bad", "doctor"}} {
		code, out, _ := executeTest(t, append(args, "--json"), "")
		if code != 2 {
			t.Fatalf("args=%v code=%d out=%s", args, code, out)
		}
		var value map[string]any
		if json.Unmarshal([]byte(out), &value) != nil || value["ok"] != false {
			t.Fatalf("invalid envelope %q", out)
		}
	}
}

func TestVideoPromptModeEquivalentAcrossEntrypoints(t *testing.T) {
	var payloads []map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		payloads = append(payloads, body)
		w.WriteHeader(202)
		_ = json.NewEncoder(w).Encode(map[string]any{"job_id": fmt.Sprintf("%032x", len(payloads))})
	}))
	defer server.Close()
	commands := []struct {
		args  []string
		input string
	}{{[]string{"--server", server.URL, "generate", "video", "--mode", "t2v", "--prompt", "x"}, ""}, {[]string{"--server", server.URL, "generate", "video", "--spec", "-"}, `{"prompt":"x","director_mode":"t2v"}`}, {[]string{"--server", server.URL, "operation", "run", "generate.video", "--input", "-"}, `{"prompt":"x","director_mode":"t2v"}`}}
	for _, item := range commands {
		code, _, stderr := executeTest(t, item.args, item.input)
		if code != 0 {
			t.Fatal(stderr)
		}
	}
	if len(payloads) != 3 {
		t.Fatalf("payloads=%v", payloads)
	}
	for _, payload := range payloads {
		if payload["prompt_mode"] != "preserve_tags_only" {
			t.Fatalf("payload=%v", payload)
		}
	}
}

func TestGenerationEntrypointsRejectInvalidPromptModeAndFlags(t *testing.T) {
	tests := []struct {
		args  []string
		input string
	}{{[]string{"generate", "video", "--mode", "t2v", "--prompt", "x", "--prompt-mode", "rewrite"}, ""}, {[]string{"generate", "video", "--spec", "-"}, `{"prompt":"x","director_mode":"t2v","prompt_mode":"rewrite"}`}, {[]string{"operation", "run", "generate.video", "--input", "-"}, `{"prompt":"x","director_mode":"t2v","prompt_mode":"rewrite"}`}, {[]string{"generate", "image", "--prompt", "x", "--duration", "5"}, ""}, {[]string{"generate", "video", "--mode", "t2v", "--prompt", "x", "--negative-prompt", "no"}, ""}, {[]string{"generate", "image", "--spec", "-", "--steps", "2"}, `{"prompt":"x"}`}, {[]string{"generate", "video", "--mode", "fl2v", "--prompt", "x", "--ref", `json:{"role":"first_frame","source":"asset:a"}`, "--ref", `json:{"role":"first_frame","source":"asset:b"}`}, ""}, {[]string{"generate", "video", "--mode", "t2v", "--prompt", "x", "--duration", "-1"}, ""}}
	for _, test := range tests {
		code, _, _ := executeTest(t, append(test.args, "--json"), test.input)
		if code != 2 {
			t.Fatalf("args=%v code=%d", test.args, code)
		}
	}
}

func TestOperationSchemaValidationAndRequestID(t *testing.T) {
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&payload)
		w.WriteHeader(202)
		_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
	}))
	defer server.Close()
	valid := `{"prompt":"x","director_mode":"t2v","references":[]}`
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "--request-id", testMediaID, "operation", "run", "generate.video", "--input", "-"}, valid)
	if code != 0 {
		t.Fatal(stderr)
	}
	if payload["request_id"] != testMediaID {
		t.Fatalf("payload=%v", payload)
	}
	invalid := []string{`{"source":"job:j","position":"first","extra":1}`, `{"source":1,"position":"first"}`, `{"source":"job:j","position":"middle"}`, `{"job_id":"j","to":"x","index":1.5}`}
	names := []string{"media.frame", "media.frame", "media.frame", "job.download"}
	for i, input := range invalid {
		code, _, _ := executeTest(t, []string{"operation", "run", names[i], "--input", "-", "--json"}, input)
		if code != 2 {
			t.Fatalf("input=%s code=%d", input, code)
		}
	}
}

func TestOperationParameterTypoFailsLocallyWithoutAnyRequest(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	input := `{"prompt":"x","director_mode":"t2v","parameters":{"duraton":5}}`
	code, out, stderr := executeTest(t, []string{"--server", server.URL, "--json", "operation", "run", "generate.video", "--input", "-"}, input)
	if code != 2 || stderr != "" || requests.Load() != 0 || !strings.Contains(out, "duraton") {
		t.Fatalf("code=%d requests=%d out=%q stderr=%q", code, requests.Load(), out, stderr)
	}
}

func TestOperationSchemaPublishesEnforcedDefaults(t *testing.T) {
	code, out, stderr := executeTest(t, []string{"operation", "schema", "generate.video", "--json"}, "")
	if code != 0 || stderr != "" {
		t.Fatalf("code=%d stderr=%q", code, stderr)
	}
	var envelope map[string]any
	if err := json.Unmarshal([]byte(out), &envelope); err != nil {
		t.Fatal(err)
	}
	schema := envelope["data"].(map[string]any)["input_schema"].(map[string]any)
	properties := schema["properties"].(map[string]any)
	if schema["additionalProperties"] != false || properties["prompt_mode"].(map[string]any)["default"] != "preserve_tags_only" {
		t.Fatalf("schema=%v", schema)
	}
}

func TestOperationGenerateUploadsLocalReference(t *testing.T) {
	file := filepath.Join(t.TempDir(), "ref.png")
	_ = os.WriteFile(file, []byte("image"), 0o600)
	uploads := 0
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/assets" {
			uploads++
			w.WriteHeader(201)
			_, _ = io.WriteString(w, `{"asset_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`)
			return
		}
		_ = json.NewDecoder(r.Body).Decode(&payload)
		w.WriteHeader(202)
		_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
	}))
	defer server.Close()
	input := fmt.Sprintf(`{"prompt":"x","director_mode":"i2v","references":[{"source":%q,"role":"first_frame"}]}`, file)
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "operation", "run", "generate.video", "--input", "-"}, input)
	if code != 0 {
		t.Fatal(stderr)
	}
	if uploads != 1 || payload["references"].([]any)[0].(map[string]any)["asset_id"] != testAssetID {
		t.Fatalf("uploads=%d payload=%v", uploads, payload)
	}
}

func TestGenerateWaitTimeoutEmitsSubmittedAndRecoveryDetails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/generate" {
			w.WriteHeader(202)
			_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "running"})
	}))
	defer server.Close()
	code, out, stderr := executeTest(t, []string{"--server", server.URL, "--output", "jsonl", "--request-id", testMediaID, "generate", "video", "--mode", "t2v", "--prompt", "x", "--wait", "--wait-timeout", "5ms", "--poll-interval", "1ms"}, "")
	if code != 5 || stderr != "" {
		t.Fatalf("code=%d stderr=%q out=%s", code, stderr, out)
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) < 2 {
		t.Fatalf("events=%q", out)
	}
	var submitted, failed map[string]any
	_ = json.Unmarshal([]byte(lines[0]), &submitted)
	_ = json.Unmarshal([]byte(lines[len(lines)-1]), &failed)
	if submitted["type"] != "submitted" {
		t.Fatalf("first=%v", submitted)
	}
	details := failed["error"].(map[string]any)["details"].(map[string]any)
	if details["job_id"] != testJobID || details["request_id"] != testMediaID || details["submission"] == nil {
		t.Fatalf("details=%v", details)
	}
}

func TestGenerateDownloadFailureKeepsSubmissionDetails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/generate":
			w.WriteHeader(202)
			_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
		case "/api/status":
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "completed"})
		case "/api/download":
			w.WriteHeader(500)
			_, _ = io.WriteString(w, `{"error":{"code":"disk","message":"failed"}}`)
		}
	}))
	defer server.Close()
	to := filepath.Join(t.TempDir(), "out.mp4")
	code, out, _ := executeTest(t, []string{"--server", server.URL, "--json", "generate", "video", "--mode", "t2v", "--prompt", "x", "--download", to}, "")
	if code == 0 {
		t.Fatal("download failure succeeded")
	}
	var envelope map[string]any
	_ = json.Unmarshal([]byte(out), &envelope)
	details := envelope["error"].(map[string]any)["details"].(map[string]any)
	if details["job_id"] != testJobID || details["submission"] == nil {
		t.Fatalf("details=%v", details)
	}
}

func TestDefaultAssetDownloadNameCannotEscapeCWD(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/content") {
			_, _ = io.WriteString(w, "x")
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"display_name": "../../escape.bin"})
	}))
	defer server.Close()
	cwd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	temp := t.TempDir()
	if err := os.Chdir(temp); err != nil {
		t.Fatal(err)
	}
	defer os.Chdir(cwd)
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "asset", "download", "asset:" + testAssetID}, "")
	if code != 0 {
		t.Fatal(stderr)
	}
	if _, err := os.Stat(filepath.Join(temp, testAssetID)); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(temp), "escape.bin")); !os.IsNotExist(err) {
		t.Fatal("download escaped cwd")
	}
}

func TestSafeDownloadNameRejectsCrossPlatformUnsafeNames(t *testing.T) {
	for _, value := range []string{"", "/", ".", "..", "folder/..", `bad:name.mp4`, `bad\\name.mp4`, "CON", "con.txt", "CON.backup.txt", "COM1.backup.txt", "LPT9.mov", "trail. ", "bad?.png"} {
		if got := safeDownloadName(value, "fallback.bin"); got != "fallback.bin" {
			t.Errorf("value=%q got=%q", value, got)
		}
	}
}

func TestBareReferenceLocatorMayContainEqualsAndComma(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "a=b,c.png")
	_ = os.WriteFile(file, []byte("image"), 0o600)
	uploaded := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/assets" {
			uploaded++
			w.WriteHeader(http.StatusCreated)
			_, _ = io.WriteString(w, `{"asset_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`)
			return
		}
		w.WriteHeader(http.StatusAccepted)
		_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
	}))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "generate", "image", "--prompt", "x", "--ref", file}, "")
	if code != 0 || uploaded != 1 {
		t.Fatalf("code=%d uploaded=%d stderr=%s", code, uploaded, stderr)
	}
	structured := "json:" + fmt.Sprintf(`{"role":"reference","source":%q}`, file)
	parsed, err := parseReference(structured)
	if err != nil || parsed.Source != file {
		t.Fatalf("parsed=%v err=%v", parsed, err)
	}
}

func TestRefDirJSONSupportsCommaInDirectory(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "refs,with,commas")
	if err := os.Mkdir(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	raw := "json:" + fmt.Sprintf(`{"role":"identity","path":%q}`, directory)
	role, path, err := parseRefDir(raw)
	if err != nil || role != "identity" || path != directory {
		t.Fatalf("role=%q path=%q err=%v", role, path, err)
	}
	if _, _, err := parseRefDir(`json:{"role":"identity","path":"x","extra":true}`); err == nil {
		t.Fatal("unknown structured ref-dir field was accepted")
	}
}

func TestCompletionHelpSucceeds(t *testing.T) {
	code, out, _ := executeTest(t, []string{"completion", "--help"}, "")
	if code != 0 || !strings.Contains(out, "Usage:") {
		t.Fatalf("code=%d out=%q", code, out)
	}
}

func TestEveryZeroFlagLeafHelpIncludesUsageAndExample(t *testing.T) {
	for _, args := range [][]string{{"version", "--help"}, {"doctor", "--help"}, {"completion", "--help"}} {
		code, out, stderr := executeTest(t, args, "")
		if code != 0 || stderr != "" || !strings.Contains(out, "Usage:") || !strings.Contains(out, "Example") {
			t.Fatalf("args=%v code=%d out=%q stderr=%q", args, code, out, stderr)
		}
	}
}

func TestStrictUnexpectedPositionalsAndNumericFlags(t *testing.T) {
	for _, args := range [][]string{
		{"asset", "list", "extra"},
		{"job", "list", "extra"},
		{"project", "create", "extra", "--spec", "-"},
		{"asset", "upload", "missing", "--kind", "document"},
		{"job", "download", "j", "--index", "-1", "--to", "x"},
		{"media", "frame", "asset:a", "--position", "current", "--at", "NaN"},
		{"media", "trim", "asset:a", "--start", "NaN", "--end", "1"},
	} {
		code, _, _ := executeTest(t, append(args, "--json"), `{}`)
		if code != 2 {
			t.Fatalf("args=%v code=%d", args, code)
		}
	}
}

func TestSpecV2VUploadsBareLocalSourceAndCommonShapeValidationIsPreflight(t *testing.T) {
	dir := t.TempDir()
	previous, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	defer os.Chdir(previous)
	if err := os.WriteFile("clip.mp4", []byte("video"), 0o600); err != nil {
		t.Fatal(err)
	}
	uploads, generates := 0, 0
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/assets":
			uploads++
			w.WriteHeader(http.StatusCreated)
			_, _ = io.WriteString(w, `{"asset_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`)
		case "/api/generate":
			generates++
			_ = json.NewDecoder(r.Body).Decode(&payload)
			w.WriteHeader(http.StatusAccepted)
			_, _ = io.WriteString(w, `{"job_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}`)
		}
	}))
	defer server.Close()
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "generate", "video", "--spec", "-"}, `{"prompt":"x","director_mode":"v2v","source_asset_id":"clip.mp4"}`)
	if code != 0 || uploads != 1 || generates != 1 || payload["source_asset_id"] != testAssetID {
		t.Fatalf("code=%d uploads=%d generates=%d payload=%v stderr=%s", code, uploads, generates, payload, stderr)
	}
	code, _, _ = executeTest(t, []string{"--server", server.URL, "operation", "run", "generate.video", "--input", "-", "--json"}, `{"prompt":"x","director_mode":"t2v","references":[{"source":"clip.mp4"}]}`)
	if code != 2 || uploads != 1 || generates != 1 {
		t.Fatalf("invalid shape made network calls: code=%d uploads=%d generates=%d", code, uploads, generates)
	}
}

func TestTypedSpecAndOperationV2VPayloadsPassRealPythonWorkflowParser(t *testing.T) {
	projectRoot, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	profileCommand := exec.Command("python3", "-c", `import json; from server.profiles import DEFAULT_REGISTRY; print(json.dumps(DEFAULT_REGISTRY.get("minimax-h3-ref2va").public()))`)
	profileCommand.Dir = projectRoot
	profileRaw, err := profileCommand.Output()
	if err != nil {
		t.Fatal(err)
	}
	var profile map[string]any
	if err := json.Unmarshal(profileRaw, &profile); err != nil {
		t.Fatal(err)
	}
	var payloads []map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/capabilities" {
			_ = json.NewEncoder(w).Encode(map[string]any{"video": map[string]any{"available": true}, "image": map[string]any{"available": true}, "profiles": []any{profile}})
			return
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		payloads = append(payloads, body)
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(map[string]any{"job_id": strings.Repeat("f", 32)})
	}))
	defer server.Close()
	sourceID := strings.Repeat("c", 32)
	imageID := strings.Repeat("a", 32)
	profileID := "minimax-h3-ref2va"
	commands := []struct {
		args  []string
		input string
	}{
		{[]string{"--server", server.URL, "generate", "video", "--mode", "v2v", "--prompt", "x", "--profile", profileID, "--source-video", "asset:" + sourceID}, ""},
		{[]string{"--server", server.URL, "generate", "video", "--spec", "-"}, fmt.Sprintf(`{"prompt":"x","director_mode":"v2v","profile_id":%q,"source_asset_id":"asset:%s"}`, profileID, sourceID)},
		{[]string{"--server", server.URL, "operation", "run", "generate.video", "--input", "-"}, fmt.Sprintf(`{"prompt":"x","director_mode":"v2v","profile_id":%q,"source_asset_id":"asset:%s"}`, profileID, sourceID)},
		{[]string{"--server", server.URL, "generate", "video", "--mode", "rv2v", "--prompt", "x", "--profile", profileID, "--source-video", "asset:" + sourceID, "--ref", fmt.Sprintf(`json:{"role":"identity","source":"asset:%s"}`, imageID)}, ""},
		{[]string{"--server", server.URL, "generate", "video", "--spec", "-"}, fmt.Sprintf(`{"prompt":"x","director_mode":"rv2v","profile_id":%q,"source_asset_id":"asset:%s","references":[{"asset_id":"%s","role":"identity"}]}`, profileID, sourceID, imageID)},
		{[]string{"--server", server.URL, "operation", "run", "generate.video", "--input", "-"}, fmt.Sprintf(`{"prompt":"x","director_mode":"rv2v","profile_id":%q,"source_asset_id":"asset:%s","references":[{"asset_id":"%s","role":"identity"}]}`, profileID, sourceID, imageID)},
	}
	for _, item := range commands {
		code, _, stderr := executeTest(t, item.args, item.input)
		if code != 0 {
			t.Fatalf("args=%v code=%d stderr=%s", item.args, code, stderr)
		}
	}
	if len(payloads) != 6 {
		t.Fatalf("payloads=%v", payloads)
	}
	encoded, _ := json.Marshal(payloads)
	bridge := exec.Command("python3", "-c", `
import json, sys
from server.workflows import parse_generation_request
source = "c" * 32
image = "a" * 32
assets = {
    source: {"id": source, "kind": "video", "filename": "source.mp4", "comfy_path": "h3-studio/source.mp4", "media": {"duration": 5, "has_audio": False, "fps": 24, "reference_fps": 24}},
    image: {"id": image, "kind": "image", "filename": "image.png", "comfy_path": "h3-studio/image.png", "media": {"width": 1, "height": 1}},
}
result = []
for payload in json.load(sys.stdin):
    spec = parse_generation_request(payload, assets.__getitem__)
    result.append({"mode": spec.director_mode, "source": spec.source_asset_id, "refs": [[r.asset_id, r.role] for r in spec.references], "profile": spec.profile_id})
print(json.dumps(result))
`)
	bridge.Dir = projectRoot
	bridge.Stdin = bytes.NewReader(encoded)
	parsedRaw, err := bridge.CombinedOutput()
	if err != nil {
		t.Fatalf("real workflow parser rejected CLI payloads: %v: %s\npayloads=%s", err, parsedRaw, encoded)
	}
	var parsed []map[string]any
	if err := json.Unmarshal(parsedRaw, &parsed); err != nil {
		t.Fatal(err)
	}
	for index, item := range parsed {
		refs := item["refs"].([]any)
		first := refs[0].([]any)
		expectedMode, expectedRefs := "v2v", 1
		if index >= 3 {
			expectedMode, expectedRefs = "rv2v", 2
		}
		if item["mode"] != expectedMode || item["source"] != sourceID || item["profile"] != profileID || len(refs) != expectedRefs || first[0] != sourceID || first[1] != "motion" {
			t.Fatalf("entrypoint %d real parse=%v", index, item)
		}
		if expectedMode == "rv2v" && refs[1].([]any)[0] != imageID {
			t.Fatalf("entrypoint %d reference order=%v", index, refs)
		}
	}
}

func TestProjectWaitCancelledContextIsInterrupted(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "running"})
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	var out, stderr bytes.Buffer
	code := Execute(ctx, []string{"--server", server.URL, "--json", "project", "wait", testProjectID, "--poll-interval", "1ms"}, IOStreams{In: strings.NewReader(""), Out: &out, Err: &stderr})
	if code == 0 {
		t.Fatalf("cancelled wait succeeded: %s", out.String())
	}
	var envelope map[string]any
	_ = json.Unmarshal(out.Bytes(), &envelope)
	if envelope["error"].(map[string]any)["code"] != "interrupted" {
		t.Fatalf("envelope=%v", envelope)
	}
}

func TestProjectWaitRetriesAndRejectsInvalidStates(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(503)
			_, _ = io.WriteString(w, `{"error":{"code":"busy","message":"retry"}}`)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "completed"})
	}))
	code, _, stderr := executeTest(t, []string{"--server", server.URL, "project", "wait", testProjectID, "--timeout", "1s", "--poll-interval", "1ms"}, "")
	server.Close()
	if code != 0 {
		t.Fatalf("code=%d stderr=%s", code, stderr)
	}
	if calls < 2 {
		t.Fatal("project wait did not retry")
	}
	code, _, _ = executeTest(t, []string{
		"--server", server.URL, "project", "wait", testProjectID, "--timeout=-1s", "--json",
	}, "")
	if code != 2 {
		t.Fatalf("negative timeout code=%d", code)
	}
	unknown := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "mystery"})
	}))
	defer unknown.Close()
	code, _, _ = executeTest(t, []string{"--server", unknown.URL, "project", "wait", testProjectID, "--timeout", "1s", "--poll-interval", "1ms", "--json"}, "")
	if code == 0 {
		t.Fatal("unknown project state succeeded")
	}
}

func TestAssetCopyAndProjectDownloadInterspersedFlags(t *testing.T) {
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/content") {
			_, _ = io.WriteString(w, "asset")
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"filename": "a.bin", "kind": "video"})
	}))
	defer source.Close()
	uploads := 0
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		uploads++
		w.WriteHeader(201)
		_, _ = io.WriteString(w, `{"asset_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`)
	}))
	defer destination.Close()
	configPath := filepath.Join(t.TempDir(), "config.json")
	t.Setenv("H3CTL_CONFIG", configPath)
	for _, item := range []struct{ name, url string }{{"source", source.URL}, {"dev", destination.URL}} {
		code, _, stderr := executeTest(t, []string{"context", "add", item.name, "--server", item.url}, "")
		if code != 0 {
			t.Fatal(stderr)
		}
	}
	code, _, stderr := executeTest(t, []string{"asset", "copy", "h3://source/assets/" + testAssetID, "--to-context", "dev"}, "")
	if code != 0 || uploads != 1 {
		t.Fatalf("code=%d uploads=%d stderr=%s", code, uploads, stderr)
	}
	project := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, "video") }))
	defer project.Close()
	to := filepath.Join(t.TempDir(), "merged.mp4")
	code, _, stderr = executeTest(t, []string{"--server", project.URL, "project", "download", testProjectID, "--to", to}, "")
	if code != 0 {
		t.Fatal(stderr)
	}
	raw, _ := os.ReadFile(to)
	if string(raw) != "video" {
		t.Fatalf("download=%q", raw)
	}
}
