package operation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	jsonschema "github.com/santhosh-tekuri/jsonschema/v6"

	"h3studio/cli/internal/api"
	"h3studio/cli/internal/contract"
)

func decodeObject(t *testing.T, raw string) map[string]any {
	t.Helper()
	value := map[string]any{}
	if err := json.Unmarshal([]byte(raw), &value); err != nil {
		t.Fatal(err)
	}
	return value
}

func TestRegistryDeepSchemaRejectsNestedTyposTypesAndRanges(t *testing.T) {
	tests := []struct {
		name, input string
	}{
		{"generate.video", `{"prompt":"x","director_mode":"t2v","parameters":{"duraton":5}}`},
		{"generate.video", `{"prompt":"x","director_mode":"r2v","references":[{"source":"asset:a","asset_id":"a","role":"identity"}]}`},
		{"generate.video", `{"prompt":"x","director_mode":"t2v","parameters":{"duration":"5"}}`},
		{"generate.video", `{"prompt":"x","director_mode":"t2v","parameters":{"duration":4}}`},
		{"generate.image", `{"prompt":"x","parameters":{"cfg":31}}`},
		{"project.run", `{"project_id":"p","segment_ids":[1]}`},
		{"project.run", `{"project_id":"p","segment_ids":["s","s"]}`},
		{"video.character_migration.plan", `{"version":"h3.character-migration/v1","source":"asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","targets":[{"character":"asset:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","source_subjet":"the centered dancer"}]}`},
		{"video.character_migration.plan", `{"version":"h3.character-migration/v1","source":"asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","targets":[]}`},
		{"video.character_migration.plan", `{"version":"h3.character-migration/v1","source":"asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","targets":[{"character":"asset:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","source_subject":"the centered dancer"}],"segment_frames":125}`},
	}
	for _, test := range tests {
		if err := ValidateInput(test.name, decodeObject(t, test.input)); err == nil {
			t.Errorf("%s accepted %s", test.name, test.input)
		}
	}
}

func TestPublishedSchemaDescribesNestedContracts(t *testing.T) {
	schema, ok := Schema("generate.video")
	if !ok {
		t.Fatal("missing generate.video")
	}
	properties := schema["properties"].(map[string]any)
	references := properties["references"].(map[string]any)
	items := references["items"].(map[string]any)
	parameters := properties["parameters"].(map[string]any)
	if len(items["oneOf"].([]any)) != 3 || parameters["additionalProperties"] != false {
		t.Fatalf("schema=%v", schema)
	}
	if references["maxItems"] != float64(12) {
		t.Fatalf("video references=%v", references)
	}
	imageSchema, _ := Schema("generate.image")
	imageReferences := imageSchema["properties"].(map[string]any)["references"].(map[string]any)
	if imageReferences["maxItems"] != float64(10) {
		t.Fatalf("image references=%v", imageReferences)
	}
	if _, ok := parameters["properties"].(map[string]any)["duration"]; !ok {
		t.Fatal("duration contract missing")
	}
	project, _ := Schema("project.run")
	segment := project["properties"].(map[string]any)["segment_ids"].(map[string]any)
	if segment["items"].(map[string]any)["type"] != "string" {
		t.Fatalf("segment schema=%v", segment)
	}
}

func TestVideoReferenceSchemaAcceptsTwelveAndRejectsThirteen(t *testing.T) {
	input := map[string]any{"prompt": "x", "director_mode": "r2v"}
	references := make([]any, 12)
	for index := range references {
		references[index] = map[string]any{
			"asset_id": fmt.Sprintf("%032x", index+1), "role": "identity", "reference_index": float64(index),
		}
	}
	input["references"] = references
	if err := ValidateInput("generate.video", input); err != nil {
		t.Fatalf("twelve references were rejected: %v", err)
	}
	input["references"] = append(references, map[string]any{
		"asset_id": strings.Repeat("f", 32), "role": "identity", "reference_index": float64(12),
	})
	if err := ValidateInput("generate.video", input); err == nil {
		t.Fatal("thirteen references were accepted")
	}
}

func TestEveryPublishedSchemaCompilesAsDraft202012(t *testing.T) {
	for _, definition := range Definitions() {
		t.Run(definition.Name, func(t *testing.T) {
			compiler := jsonschema.NewCompiler()
			location := "https://h3ctl.invalid/operations/" + definition.Name
			if err := compiler.AddResource(location, definition.InputSchema); err != nil {
				t.Fatal(err)
			}
			if _, err := compiler.Compile(location); err != nil {
				t.Fatalf("invalid Draft 2020-12 schema: %v\nschema=%#v", err, definition.InputSchema)
			}
		})
	}
}

func TestRegistryAndExecuteCoverWorkflowAtoms(t *testing.T) {
	required := []string{
		"asset.copy", "media.endpoints", "media.prepare_reference", "media.mux_audio",
		"media.save", "media.download", "project.create", "project.apply", "project.list",
		"project.get", "project.wait", "project.run", "project.merge", "project.download",
		"video.compose", "video.character_migration.plan", "video.character_migration.produce",
		"job.list", "job.get", "job.wait", "job.resume", "job.cancel", "job.download",
		"job.save", "job.delete", "generate.image", "generate.video",
	}
	names := map[string]bool{}
	for _, definition := range Definitions() {
		names[definition.Name] = true
	}
	for _, name := range required {
		if !names[name] {
			t.Errorf("missing operation %s", name)
		}
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/media/derive" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"receipt_id": strings.Repeat("d", 32)})
			return
		}
		if strings.HasSuffix(r.URL.Path, "/download") {
			_, _ = io.WriteString(w, "media")
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "completed"})
	}))
	defer server.Close()
	runtime := Runtime{Service: &Service{API: api.New(server.URL, time.Second), Context: "test", PollInterval: time.Millisecond}, CopyAsset: func(_ context.Context, source, destination string) (map[string]any, error) {
		return map[string]any{"source": source, "destination_context": destination}, nil
	}}
	value, err := Execute(context.Background(), runtime, "asset.copy", decodeObject(t, `{"source":"h3://src/assets/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","to_context":"dev"}`))
	if err != nil || value.(map[string]any)["destination_context"] != "dev" {
		t.Fatalf("copy value=%v err=%v", value, err)
	}
	value, err = Execute(context.Background(), runtime, "media.endpoints", decodeObject(t, `{"source":"asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`))
	if err != nil || value.(map[string]any)["first"] == nil || value.(map[string]any)["last"] == nil {
		t.Fatalf("endpoints value=%v err=%v", value, err)
	}
	to := filepath.Join(t.TempDir(), "media.bin")
	if _, err := Execute(context.Background(), runtime, "media.download", map[string]any{"media_id": strings.Repeat("d", 32), "to": to}); err != nil {
		t.Fatal(err)
	}
}

func TestEveryPublishedOperationExecutesAndRejectsUnknownInput(t *testing.T) {
	idA, idB, idC, idD := strings.Repeat("a", 32), strings.Repeat("b", 32), strings.Repeat("c", 32), strings.Repeat("d", 32)
	type capturedRequest struct {
		method, path string
		body         map[string]any
	}
	var captured []capturedRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := map[string]any{}
		if strings.Contains(r.Header.Get("Content-Type"), "application/json") && r.Body != nil {
			_ = json.NewDecoder(r.Body).Decode(&body)
		}
		captured = append(captured, capturedRequest{method: r.Method, path: r.URL.RequestURI(), body: body})
		switch {
		case r.URL.Path == "/api/assets" && r.Method == http.MethodPost:
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"asset_id": idA})
		case r.URL.Path == "/api/generate":
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"job_id": idB})
		case r.URL.Path == "/api/voice/tasks" && r.Method == http.MethodPost:
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"task_id": idC, "status": "queued"})
		case strings.HasPrefix(r.URL.Path, "/api/voice/tasks/") && r.Method == http.MethodGet && !strings.HasSuffix(r.URL.Path, "/download"):
			_ = json.NewEncoder(w).Encode(map[string]any{"task_id": idC, "status": "completed"})
		case r.URL.Path == "/api/status":
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "completed", "job_id": idB})
		case strings.HasSuffix(r.URL.Path, "/resume"):
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"job_id": idB, "status": "queued"})
		case r.URL.Path == "/api/media/derive":
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"receipt_id": idC})
		case r.URL.Path == "/api/media/mux-audio":
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"receipt_id": idC})
		case r.URL.Path == "/api/video/character-migration/plan":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"version": "h3.character-migration/v1",
				"project": map[string]any{"title": "migration", "segments": []any{}},
			})
		case strings.HasSuffix(r.URL.Path, "/assets"):
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]any{"asset_id": idA})
		case strings.Contains(r.URL.Path, "/download") || strings.HasSuffix(r.URL.Path, "/content"):
			_, _ = io.WriteString(w, "download")
		case r.URL.Path == "/api/video-projects/"+idD && r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(map[string]any{"id": idD, "status": "completed"})
		default:
			_ = json.NewEncoder(w).Encode(map[string]any{"id": idD, "asset_id": idA, "status": "ok"})
		}
	}))
	defer server.Close()
	temp := t.TempDir()
	upload := filepath.Join(temp, "upload.bin")
	if err := os.WriteFile(upload, []byte("upload"), 0o600); err != nil {
		t.Fatal(err)
	}
	copyCalls := 0
	runtime := Runtime{
		Service: &Service{API: api.NewWithTimeouts(server.URL, time.Second, time.Second, time.Second), Context: "test", PollInterval: time.Millisecond},
		CopyAsset: func(_ context.Context, source, destination string) (map[string]any, error) {
			copyCalls++
			return map[string]any{"source": source, "destination_context": destination}, nil
		},
	}
	tests := []struct {
		name, input, method, path, bodyKey string
		bodyValue                          any
		calls                              int
	}{
		{"asset.upload", fmt.Sprintf(`{"path":%q,"kind":"video"}`, upload), "POST", "/api/assets", "", nil, 1},
		{"asset.download", fmt.Sprintf(`{"asset_id":%q,"to":%q}`, idA, filepath.Join(temp, "asset.bin")), "GET", "/api/assets/" + idA + "/content", "", nil, 1},
		{"asset.list", `{}`, "GET", "/api/assets", "", nil, 1},
		{"asset.copy", fmt.Sprintf(`{"source":"h3://source/assets/%s","to_context":"dev"}`, idA), "", "", "", nil, 0},
		{"generate.image", `{"prompt":"image"}`, "POST", "/api/generate", "output_type", "image", 1},
		{"generate.video", `{"prompt":"video","director_mode":"t2v"}`, "POST", "/api/generate", "output_type", "video", 1},
		{"job.list", `{}`, "GET", "/api/jobs?limit=20", "", nil, 1},
		{"job.get", fmt.Sprintf(`{"job_id":%q}`, idB), "GET", "/api/jobs/" + idB, "", nil, 1},
		{"job.wait", fmt.Sprintf(`{"job_id":%q,"timeout_seconds":1,"poll_seconds":0.001}`, idB), "GET", "/api/status?id=" + idB, "", nil, 1},
		{"job.cancel", fmt.Sprintf(`{"job_id":%q}`, idB), "POST", "/api/jobs/" + idB + "/cancel", "", nil, 1},
		{"job.download", fmt.Sprintf(`{"job_id":%q,"to":%q}`, idB, filepath.Join(temp, "job.bin")), "GET", "/api/download?id=" + idB + "&index=0", "", nil, 1},
		{"job.save", fmt.Sprintf(`{"job_id":%q}`, idB), "POST", "/api/jobs/" + idB + "/assets", "visibility", "library", 1},
		{"job.workflow", fmt.Sprintf(`{"job_id":%q}`, idB), "GET", "/api/jobs/" + idB + "/workflow", "", nil, 1},
		{"job.resume", fmt.Sprintf(`{"job_id":%q,"additional_steps":2}`, idB), "POST", "/api/jobs/" + idB + "/resume", "additional_steps", float64(2), 1},
		{"job.delete", fmt.Sprintf(`{"job_id":%q}`, idB), "DELETE", "/api/jobs/" + idB, "", nil, 1},
		{"media.frame", fmt.Sprintf(`{"source":"asset:%s","position":"first"}`, idA), "POST", "/api/media/derive", "position", "first", 1},
		{"media.endpoints", fmt.Sprintf(`{"source":"asset:%s"}`, idA), "POST", "/api/media/derive", "position", "last", 2},
		{"media.trim", fmt.Sprintf(`{"source":"asset:%s","start":0,"end":1}`, idA), "POST", "/api/media/derive", "operation", "video_trim", 1},
		{"media.extract_audio", fmt.Sprintf(`{"source":"asset:%s"}`, idA), "POST", "/api/media/derive", "operation", "extract_audio", 1},
		{"media.remove_audio", fmt.Sprintf(`{"source":"asset:%s"}`, idA), "POST", "/api/media/derive", "operation", "remove_audio", 1},
		{"media.mux_audio", fmt.Sprintf(`{"video":"asset:%s","audio":"asset:%s"}`, idA, idB), "POST", "/api/media/mux-audio", "", nil, 1},
		{"media.prepare_reference", fmt.Sprintf(`{"source":"asset:%s","preset":"h3-low-token"}`, idA), "POST", "/api/media/derive", "operation", "prepare_h3_reference", 1},
		{"media.list", `{}`, "GET", "/api/derivations", "", nil, 1},
		{"media.get", fmt.Sprintf(`{"media_id":%q}`, idC), "GET", "/api/derivations/" + idC, "", nil, 1},
		{"media.download", fmt.Sprintf(`{"media_id":%q,"to":%q}`, idC, filepath.Join(temp, "media.bin")), "GET", "/api/derivations/" + idC + "/download", "", nil, 1},
		{"media.save", fmt.Sprintf(`{"media_id":%q}`, idC), "POST", "/api/derivations/" + idC + "/assets", "visibility", "library", 1},
		{"media.delete", fmt.Sprintf(`{"media_id":%q}`, idC), "DELETE", "/api/derivations/" + idC, "", nil, 1},
		{"voice.convert", fmt.Sprintf(`{"engine":"vevo2","source":"asset:%s","reference":"asset:%s"}`, idA, idB), "POST", "/api/voice/tasks", "engine", "vevo2", 1},
		{"voice.get", fmt.Sprintf(`{"task_id":%q}`, idC), "GET", "/api/voice/tasks/" + idC, "", nil, 1},
		{"voice.wait", fmt.Sprintf(`{"task_id":%q,"timeout_seconds":1,"poll_seconds":0.001}`, idC), "GET", "/api/voice/tasks/" + idC, "", nil, 1},
		{"voice.cancel", fmt.Sprintf(`{"task_id":%q}`, idC), "POST", "/api/voice/tasks/" + idC + "/cancel", "", nil, 1},
		{"voice.delete", fmt.Sprintf(`{"task_id":%q}`, idC), "DELETE", "/api/voice/tasks/" + idC, "", nil, 1},
		{"voice.download", fmt.Sprintf(`{"task_id":%q,"to":%q}`, idC, filepath.Join(temp, "voice.wav")), "GET", "/api/voice/tasks/" + idC + "/download", "", nil, 1},
		{"gpu.status", `{}`, "GET", "/api/resources/gpus", "", nil, 1},
		{"project.create", `{"spec":{"title":"project"}}`, "POST", "/api/video-projects", "title", "project", 1},
		{"project.apply", fmt.Sprintf(`{"project_id":%q,"spec":{"title":"project"}}`, idD), "PUT", "/api/video-projects/" + idD, "title", "project", 1},
		{"project.list", `{}`, "GET", "/api/video-projects", "", nil, 1},
		{"project.get", fmt.Sprintf(`{"project_id":%q}`, idD), "GET", "/api/video-projects/" + idD, "", nil, 1},
		{"project.delete", fmt.Sprintf(`{"project_id":%q}`, idD), "DELETE", "/api/video-projects/" + idD, "", nil, 1},
		{"project.run", fmt.Sprintf(`{"project_id":%q,"segment_ids":[%q]}`, idD, idA), "POST", "/api/video-projects/" + idD + "/run", "segment_ids", []any{idA}, 1},
		{"project.wait", fmt.Sprintf(`{"project_id":%q,"timeout_seconds":1,"poll_seconds":0.001}`, idD), "GET", "/api/video-projects/" + idD, "", nil, 1},
		{"project.stop", fmt.Sprintf(`{"project_id":%q}`, idD), "POST", "/api/video-projects/" + idD + "/stop", "", nil, 1},
		{"project.rerun", fmt.Sprintf(`{"project_id":%q,"segment_id":%q}`, idD, idA), "POST", "/api/video-projects/" + idD + "/segments/" + idA + "/run", "", nil, 1},
		{"project.merge", fmt.Sprintf(`{"project_id":%q}`, idD), "POST", "/api/video-projects/" + idD + "/merge", "", nil, 1},
		{"project.download", fmt.Sprintf(`{"project_id":%q,"to":%q}`, idD, filepath.Join(temp, "project.bin")), "GET", "/api/video-projects/" + idD + "/merged/download", "", nil, 1},
		{"video.compose", fmt.Sprintf(`{"spec":{"title":"film","segments":[]},"to":%q,"poll_seconds":0.001}`, filepath.Join(temp, "composed.mp4")), "GET", "/api/video-projects/" + idD + "/merged/download", "", nil, 6},
		{"video.character_migration.plan", fmt.Sprintf(`{"version":"h3.character-migration/v1","source":"asset:%s","targets":[{"character":"asset:%s","source_subject":"the centered dancer"}]}`, idA, idB), "POST", "/api/video/character-migration/plan", "source_asset_id", idA, 1},
		{"video.character_migration.produce", fmt.Sprintf(`{"version":"h3.character-migration/v1","source":"asset:%s","targets":[{"character":"asset:%s","source_subject":"the centered dancer"}],"to":%q,"poll_seconds":0.001}`, idA, idB, filepath.Join(temp, "migration.mp4")), "GET", "/api/video-projects/" + idD + "/merged/download", "", nil, 7},
	}
	if len(tests) != len(Definitions()) {
		t.Fatalf("execution matrix has %d cases for %d definitions", len(tests), len(Definitions()))
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			input := decodeObject(t, test.input)
			before, beforeCopy := len(captured), copyCalls
			if _, err := Execute(context.Background(), runtime, test.name, input); err != nil {
				t.Fatalf("positive execution failed: %v", err)
			}
			if len(captured)-before != test.calls {
				t.Fatalf("requests=%v", captured[before:])
			}
			if test.calls > 0 {
				last := captured[len(captured)-1]
				if last.method != test.method || last.path != test.path {
					t.Fatalf("got %s %s, want %s %s", last.method, last.path, test.method, test.path)
				}
				if test.bodyKey != "" && fmt.Sprint(last.body[test.bodyKey]) != fmt.Sprint(test.bodyValue) {
					t.Fatalf("body=%v, want %s=%v", last.body, test.bodyKey, test.bodyValue)
				}
			} else if copyCalls != beforeCopy+1 {
				t.Fatal("local copy behavior was not invoked")
			}
			negative := decodeObject(t, test.input)
			negative["unexpected"] = true
			before, beforeCopy = len(captured), copyCalls
			if _, err := Execute(context.Background(), runtime, test.name, negative); err == nil {
				t.Fatal("unknown input was accepted")
			}
			if len(captured) != before || copyCalls != beforeCopy {
				t.Fatal("negative input performed external work")
			}
		})
	}
}

func TestVoiceConvertOperationPassesYingMusicTuning(t *testing.T) {
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/voice/tasks" {
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&payload)
		w.WriteHeader(http.StatusAccepted)
		_ = json.NewEncoder(w).Encode(map[string]any{"task_id": "cccccccccccccccccccccccccccccccc", "status": "queued"})
	}))
	defer server.Close()
	runtime := Runtime{Service: &Service{API: api.New(server.URL, time.Second), Context: "test"}}
	input := decodeObject(t, `{"engine":"yingmusic","source":"asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","reference":"asset:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","diffusion_steps":75,"inference_cfg_rate":0.9,"seed":42,"output_options":{"include_stems":true,"echo":false,"reverb":false}}`)
	if _, err := Execute(context.Background(), runtime, "voice.convert", input); err != nil {
		t.Fatal(err)
	}
	if payload["diffusion_steps"] != float64(75) || payload["inference_cfg_rate"] != 0.9 || payload["seed"] != float64(42) {
		t.Fatalf("payload=%v", payload)
	}
	options, ok := payload["output_options"].(map[string]any)
	if !ok || options["include_stems"] != true || options["echo"] != false || options["reverb"] != false {
		t.Fatalf("output_options=%v", payload["output_options"])
	}
}

func TestNonIdempotentCreateInvalidIDsAreNotMarkedRetryable(t *testing.T) {
	for _, test := range []struct {
		name, input string
		status      int
		response    string
	}{
		{"project.create", `{"spec":{"title":"x"}}`, http.StatusCreated, `{"id":"INVALID"}`},
		{"media.save", `{"media_id":"cccccccccccccccccccccccccccccccc"}`, http.StatusCreated, `{"asset_id":"INVALID"}`},
	} {
		t.Run(test.name, func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				w.WriteHeader(test.status)
				_, _ = io.WriteString(w, test.response)
			}))
			defer server.Close()
			runtime := Runtime{Service: &Service{API: api.New(server.URL, time.Second), Context: "test"}}
			_, err := Execute(context.Background(), runtime, test.name, decodeObject(t, test.input))
			var typed *contract.CLIError
			if !errors.As(err, &typed) || typed.Code != "invalid_response" || typed.Retryable || calls != 1 {
				t.Fatalf("calls=%d err=%#v", calls, err)
			}
		})
	}
}

func TestPrepareReferenceContextCancellationCancelsRemoteMediaTask(t *testing.T) {
	idA, taskID := strings.Repeat("a", 32), strings.Repeat("e", 32)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cancelCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/media/derive":
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"task_id": taskID, "status": "queued"})
			go func() { time.Sleep(10 * time.Millisecond); cancel() }()
		case r.Method == http.MethodGet && r.URL.Path == "/api/media-tasks/"+taskID:
			_ = json.NewEncoder(w).Encode(map[string]any{"task_id": taskID, "status": "running", "progress": 20})
		case r.Method == http.MethodPost && r.URL.Path == "/api/media-tasks/"+taskID+"/cancel":
			cancelCalls++
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]any{"task_id": taskID, "status": "cancelling"})
		default:
			t.Fatalf("unexpected %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()
	service := &Service{API: api.New(server.URL, time.Second), Context: "test", PollInterval: time.Millisecond}
	_, err := service.DeriveWithEvents(ctx, "asset:"+idA, map[string]any{"operation": "prepare_h3_reference", "preset": "h3-low-token"}, nil)
	var typed *contract.CLIError
	if !errors.As(err, &typed) || typed.Code != "cancelled" || cancelCalls != 1 {
		t.Fatalf("cancelCalls=%d err=%#v", cancelCalls, err)
	}
}
