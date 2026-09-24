package operation

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func TestReplicationImportStripsRuntimeAndAcceptsEnvelopes(t *testing.T) {
	project := map[string]any{"id": serviceReqID, "title": "Review", "status": "completed", "merged": map[string]any{"status": "completed"}, "recipe": map[string]any{"type": "replication", "version": "h3.replication/v1"}, "segments": []any{map[string]any{"id": serviceJobID, "request": map[string]any{"prompt": "exact"}, "status": "completed", "job_id": serviceJobID, "attempts": []any{}, "preview_url": "/old"}}}
	for _, value := range []map[string]any{project, {"project": project}, {"data": map[string]any{"project": project}}} {
		spec, err := ReplicationSpec(value)
		if err != nil {
			t.Fatal(err)
		}
		if spec["id"] != nil || spec["merged"] != nil || spec["status"] != nil {
			t.Fatalf("runtime leaked: %#v", spec)
		}
		shot := spec["segments"].([]any)[0].(map[string]any)
		if len(shot) != 2 || shot["job_id"] != nil {
			t.Fatalf("runtime leaked: %#v", shot)
		}
	}
	if project["status"] != "completed" {
		t.Fatal("input mutated")
	}
	if _, err := ReplicationSpec(map[string]any{}); err == nil {
		t.Fatal("accepted ordinary project")
	}
}

func TestReplicationResumeUsesExistingProjectThenMergesAndDownloads(t *testing.T) {
	var calls []string
	status := "running"
	merged := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		route := "/api/video-projects/" + serviceReqID
		switch r.URL.Path {
		case route:
			value := map[string]any{"id": serviceReqID, "status": status, "recipe": map[string]any{"type": "replication", "version": "h3.replication/v1"}, "segments": []any{map[string]any{"id": serviceJobID, "status": "completed"}}}
			if merged {
				value["merged"] = map[string]any{"status": "completed"}
			}
			_ = json.NewEncoder(w).Encode(value)
			if status == "running" {
				status = "partial"
			}
		case route + "/run":
			status = "completed"
			io.WriteString(w, `{}`)
		case route + "/merge":
			status = "merged"
			merged = true
			io.WriteString(w, `{}`)
		case route + "/merged/download":
			io.WriteString(w, "video")
		default:
			t.Errorf("unexpected route %s", r.URL.Path)
			http.Error(w, "unexpected", 500)
		}
	}))
	defer server.Close()
	destination := filepath.Join(t.TempDir(), "final.mp4")
	result, err := serviceFor(server).FinishReplication(context.Background(), serviceReqID, destination, false, WaitOptions{PollInterval: time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	route := "/api/video-projects/" + serviceReqID
	want := []string{"GET " + route, "GET " + route, "POST " + route + "/run", "GET " + route, "POST " + route + "/merge", "GET " + route, "GET " + route + "/merged/download"}
	if !reflect.DeepEqual(calls, want) {
		t.Fatalf("calls: %v", calls)
	}
	data, _ := os.ReadFile(destination)
	if string(data) != "video" || result["project_id"] != serviceReqID {
		t.Fatalf("bad result %#v", result)
	}
	calls = nil
	if _, err := serviceFor(server).FinishReplication(context.Background(), serviceReqID, destination, false, WaitOptions{}); err == nil {
		t.Fatal("overwrote existing output")
	}
	if len(calls) != 0 {
		t.Fatal("started network work before output preflight")
	}
}
