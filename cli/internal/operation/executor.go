package operation

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"h3studio/cli/internal/api"
	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/resource"
)

type Runtime struct {
	Service   *Service
	OnEvent   func(map[string]any)
	CopyAsset func(context.Context, string, string) (map[string]any, error)
}

func Execute(ctx context.Context, runtime Runtime, name string, input map[string]any) (any, error) {
	if err := ValidateInput(name, input); err != nil {
		return nil, err
	}
	s := runtime.Service
	require := func(key string) string { return stringValue(input[key], "") }
	switch name {
	case "douyin.submit":
		return s.SubmitDouyin(ctx, input)
	case "douyin.capabilities":
		return s.API.Get(ctx, "/api/douyin/capabilities")
	case "douyin.list":
		return s.API.Get(ctx, "/api/douyin/tasks")
	case "douyin.get":
		return s.API.Get(ctx, "/api/douyin/tasks/"+require("task_id"))
	case "douyin.cancel", "douyin.retry":
		return jsonAction(ctx, s, http.MethodPost, "/api/douyin/tasks/"+require("task_id")+"/"+strings.TrimPrefix(name, "douyin."), map[string]any{})
	case "douyin.wait":
		return s.WaitDouyin(ctx, require("task_id"), WaitOptions{Timeout: durationSeconds(input["timeout_seconds"]), PollInterval: durationSeconds(input["poll_seconds"]), OnEvent: runtime.OnEvent})

	case "asset.upload":
		return s.Upload(ctx, require("path"), stringValue(input["kind"], "auto"))
	case "asset.download":
		return s.API.Download(ctx, "/api/assets/"+url.PathEscape(require("asset_id"))+"/content", require("to"), boolValue(input["force"]))
	case "asset.list":
		query := url.Values{}
		if value := stringValue(input["query"], ""); value != "" {
			query.Set("q", value)
		}
		if value := stringValue(input["folder_id"], ""); value != "" {
			query.Set("folder_id", value)
		}
		return s.API.Get(ctx, api.Query("/api/assets", query))
	case "asset.copy":
		if runtime.CopyAsset == nil {
			return nil, contract.NewError("internal_error", "asset copy runtime is unavailable")
		}
		return runtime.CopyAsset(ctx, require("source"), require("to_context"))
	case "generate.image", "generate.video":
		input["output_type"] = strings.TrimPrefix(name, "generate.")
		return s.Generate(ctx, input)
	case "job.list":
		limit := intValue(input["limit"], 20)
		query := url.Values{"limit": {strconv.Itoa(limit)}}
		if value := stringValue(input["cursor"], ""); value != "" {
			query.Set("cursor", value)
		}
		if boolValue(input["results"]) {
			query.Set("results", "1")
		}
		return s.API.Get(ctx, "/api/jobs?"+query.Encode())
	case "job.get":
		return s.API.Get(ctx, "/api/jobs/"+url.PathEscape(require("job_id")))
	case "job.wait":
		return s.Wait(ctx, require("job_id"), WaitOptions{Timeout: durationSeconds(input["timeout_seconds"]), PollInterval: durationSeconds(input["poll_seconds"]), OnEvent: runtime.OnEvent})
	case "job.cancel":
		return jsonAction(ctx, s, http.MethodPost, "/api/jobs/"+url.PathEscape(require("job_id"))+"/cancel", nil)
	case "job.download":
		return s.API.Download(ctx, "/api/download?id="+url.QueryEscape(require("job_id"))+"&index="+strconv.Itoa(intValue(input["index"], 0)), require("to"), boolValue(input["force"]))
	case "job.save":
		body := map[string]any{"index": intValue(input["index"], 0), "visibility": "library"}
		copyOptional(body, input, "display_name", "folder_id")
		return jsonActionWithID(ctx, s, http.MethodPost, "/api/jobs/"+url.PathEscape(require("job_id"))+"/assets", body, "asset_id", "id")
	case "job.workflow":
		path := "/api/jobs/" + url.PathEscape(require("job_id")) + "/workflow"
		if to := stringValue(input["to"], ""); to != "" {
			return s.API.Download(ctx, path+"?download=1", to, boolValue(input["force"]))
		}
		return s.API.Get(ctx, path)
	case "job.resume":
		submitted, err := s.Resume(ctx, require("job_id"), intValue(input["additional_steps"], 0), stringValue(input["request_id"], ""))
		if err != nil {
			return nil, err
		}
		jobID := stringValue(submitted["job_id"], "")
		result := map[string]any{"submitted": submitted, "job_id": jobID, "request_id": submitted["request_id"]}
		if !boolValue(input["wait"]) && stringValue(input["download"], "") == "" {
			return result, nil
		}
		if runtime.OnEvent != nil {
			runtime.OnEvent(map[string]any{"type": "submitted", "job_id": jobID, "request_id": submitted["request_id"]})
		}
		completed, err := s.Wait(ctx, jobID, WaitOptions{Timeout: durationSeconds(input["timeout_seconds"]), PollInterval: durationSeconds(input["poll_seconds"]), OnEvent: runtime.OnEvent})
		if err != nil {
			return nil, err
		}
		result["completed"] = completed
		if destination := stringValue(input["download"], ""); destination != "" {
			downloaded, err := s.API.Download(ctx, "/api/download?id="+url.QueryEscape(jobID)+"&index=0", destination, boolValue(input["force"]))
			if err != nil {
				return nil, err
			}
			result["download"] = downloaded
		}
		return result, nil
	case "job.delete":
		return jsonAction(ctx, s, http.MethodDelete, "/api/jobs/"+url.PathEscape(require("job_id")), nil)
	case "media.frame":
		body := map[string]any{"operation": "frame", "position": require("position")}
		copyOptional(body, input, "time", "display_name")
		return s.Derive(ctx, require("source"), body)
	case "media.endpoints":
		first, err := s.Derive(ctx, require("source"), map[string]any{"operation": "frame", "position": "first"})
		if err != nil {
			return nil, err
		}
		last, err := s.Derive(ctx, require("source"), map[string]any{"operation": "frame", "position": "last"})
		if err != nil {
			return nil, &contract.CLIError{Code: "partial_failure", Message: err.Error(), Details: map[string]any{"first": first}, Cause: err}
		}
		return map[string]any{"first": first, "last": last}, nil
	case "media.trim":
		op := "video_trim"
		if boolValue(input["audio"]) {
			op = "audio_trim"
		}
		body := map[string]any{"operation": op, "start": input["start"], "end": input["end"]}
		copyOptional(body, input, "display_name")
		return s.Derive(ctx, require("source"), body)
	case "media.extract_audio", "media.remove_audio":
		body := map[string]any{"operation": strings.TrimPrefix(name, "media.")}
		copyOptional(body, input, "display_name")
		return s.Derive(ctx, require("source"), body)
	case "media.mux_audio":
		body := map[string]any{}
		copyOptional(body, input, "duration", "display_name")
		return s.MuxAudio(ctx, require("video"), require("audio"), body)
	case "media.prepare_reference":
		body := map[string]any{
			"operation":      "prepare_h3_reference",
			"max_short_edge": intValue(input["max_short_edge"], 480),
			"max_long_edge":  intValue(input["max_long_edge"], 864),
			"fps":            intValue(input["fps"], 24),
			"max_duration":   numberValue(input["max_duration"], 15),
			"fit":            stringValue(input["fit"], "contain"),
			"alignment":      intValue(input["alignment"], 32),
			"pad_mode":       stringValue(input["pad_mode"], "edge"),
		}
		copyOptional(body, input, "preset", "audio", "display_name")
		value, err := s.DeriveWithEvents(ctx, require("source"), body, runtime.OnEvent)
		if err == nil {
			value["locator"] = "media:" + stringValue(value["id"], "")
		}
		return value, err
	case "media.list":
		return s.API.Get(ctx, "/api/derivations")
	case "media.get":
		return s.API.Get(ctx, "/api/derivations/"+url.PathEscape(require("media_id")))
	case "media.download":
		return s.API.Download(ctx, "/api/derivations/"+url.PathEscape(require("media_id"))+"/download", require("to"), boolValue(input["force"]))
	case "media.save":
		body := map[string]any{"visibility": "library"}
		copyOptional(body, input, "display_name", "folder_id")
		return jsonActionWithID(ctx, s, http.MethodPost, "/api/derivations/"+url.PathEscape(require("media_id"))+"/assets", body, "asset_id", "id")
	case "media.delete":
		return jsonAction(ctx, s, http.MethodDelete, "/api/derivations/"+url.PathEscape(require("media_id")), nil)
	case "voice.rewrite", "voice.convert", "voice.transcribe":
		tuning := map[string]any{}
		copyOptional(tuning, input, "diffusion_steps", "inference_cfg_rate", "seed", "output_options")
		var submitted map[string]any
		var err error
		if name == "voice.transcribe" {
			submitted, err = s.SubmitVoice(ctx, stringValue(input["engine"], "soulx"), require("source"), require("source"), stringValue(input["request_id"], ""), map[string]any{"operation": "transcribe"})
		} else if name == "voice.rewrite" {
			submitted, err = s.SubmitRewrite(ctx, input, stringValue(input["request_id"], ""))
		} else {
			submitted, err = s.SubmitVoice(ctx, require("engine"), require("source"), require("reference"), stringValue(input["request_id"], ""), tuning)
		}
		if err != nil {
			return nil, err
		}
		taskID := stringValue(submitted["task_id"], "")
		result := map[string]any{"submitted": submitted, "task_id": taskID}
		if !boolValue(input["wait"]) && stringValue(input["download"], "") == "" {
			return result, nil
		}
		completed, err := s.WaitVoice(ctx, taskID, WaitOptions{Timeout: durationSeconds(input["timeout_seconds"]), PollInterval: durationSeconds(input["poll_seconds"]), OnEvent: runtime.OnEvent})
		if err != nil {
			return nil, err
		}
		result["completed"] = completed
		if destination := stringValue(input["download"], ""); destination != "" {
			downloaded, err := s.API.Download(ctx, voiceDownloadRoute(taskID, stringValue(input["download_track"], "mix")), destination, boolValue(input["force"]))
			if err != nil {
				return nil, err
			}
			result["download"] = downloaded
		}
		return result, nil
	case "voice.remix":
		body := map[string]any{}
		copyOptional(body, input, "vocal_gain_db", "accompaniment_gain_db")
		return jsonAction(ctx, s, http.MethodPost, "/api/voice/tasks/"+url.PathEscape(require("task_id"))+"/remix", body)
	case "voice.get":
		return s.API.Get(ctx, "/api/voice/tasks/"+url.PathEscape(require("task_id")))
	case "voice.wait":
		return s.WaitVoice(ctx, require("task_id"), WaitOptions{Timeout: durationSeconds(input["timeout_seconds"]), PollInterval: durationSeconds(input["poll_seconds"]), OnEvent: runtime.OnEvent})
	case "voice.cancel":
		return jsonAction(ctx, s, http.MethodPost, "/api/voice/tasks/"+url.PathEscape(require("task_id"))+"/cancel", nil)
	case "voice.delete":
		return jsonAction(ctx, s, http.MethodDelete, "/api/voice/tasks/"+url.PathEscape(require("task_id")), nil)
	case "voice.download":
		return s.API.Download(ctx, voiceDownloadRoute(require("task_id"), stringValue(input["track"], "mix")), require("to"), boolValue(input["force"]))
	case "gpu.status":
		return s.API.Get(ctx, "/api/resources/gpus")
	case "project.create":
		return jsonActionWithID(ctx, s, http.MethodPost, "/api/video-projects", input["spec"], "project_id", "id")
	case "project.apply":
		return jsonActionWithID(ctx, s, http.MethodPut, "/api/video-projects/"+url.PathEscape(require("project_id")), input["spec"], "project_id", "id")
	case "project.list":
		return s.API.Get(ctx, "/api/video-projects")
	case "project.get":
		return s.API.Get(ctx, "/api/video-projects/"+url.PathEscape(require("project_id")))
	case "project.delete":
		return jsonAction(ctx, s, http.MethodDelete, "/api/video-projects/"+url.PathEscape(require("project_id")), nil)
	case "project.run":
		body := map[string]any{}
		copyOptional(body, input, "segment_ids")
		return jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(require("project_id"))+"/run", body)
	case "project.wait":
		return s.WaitProject(ctx, require("project_id"), durationSeconds(input["timeout_seconds"]), durationSeconds(input["poll_seconds"]), runtime.OnEvent)
	case "project.stop", "project.merge":
		return jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(require("project_id"))+"/"+strings.TrimPrefix(name, "project."), map[string]any{})
	case "project.rerun":
		return jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(require("project_id"))+"/segments/"+url.PathEscape(require("segment_id"))+"/run", map[string]any{})
	case "project.download":
		return s.API.Download(ctx, "/api/video-projects/"+url.PathEscape(require("project_id"))+"/merged/download", require("to"), boolValue(input["force"]))
	case "video.compose":
		spec, _ := input["spec"].(map[string]any)
		return s.ComposeVideo(ctx, spec, require("to"), boolValue(input["force"]), WaitOptions{
			Timeout:      durationSeconds(input["timeout_seconds"]),
			PollInterval: durationSeconds(input["poll_seconds"]),
			OnEvent:      runtime.OnEvent,
		})
	case "video.character_migration.plan":
		return s.PlanCharacterMigration(ctx, input)
	case "video.character_migration.produce":
		return s.ProduceCharacterMigration(ctx, input, WaitOptions{
			Timeout:      durationSeconds(input["timeout_seconds"]),
			PollInterval: durationSeconds(input["poll_seconds"]),
			OnEvent:      runtime.OnEvent,
		})
	case "video.replication.create":
		return s.CreateReplication(ctx, input["plan"].(map[string]any))
	case "video.replication.inspect", "video.replication.export":
		project, err := s.GetReplication(ctx, require("project_id"))
		if err != nil {
			return nil, err
		}
		if name == "video.replication.export" {
			return ReplicationSpec(project)
		}
		return project, nil
	case "video.replication.edit_segment":
		body := map[string]any{}
		copyOptional(body, input, "expected_updated_at", "prompt", "seed", "steps")
		return jsonAction(ctx, s, http.MethodPatch, "/api/video-projects/"+url.PathEscape(require("project_id"))+"/segments/"+url.PathEscape(require("segment_id")), body)
	case "video.replication.resume":
		return s.FinishReplication(ctx, require("project_id"), require("to"), boolValue(input["force"]), WaitOptions{
			Timeout: durationSeconds(input["timeout_seconds"]), PollInterval: durationSeconds(input["poll_seconds"]), OnEvent: runtime.OnEvent,
		})

	case "video.replication.plan":
		return s.PlanReplication(ctx, input)
	case "video.replication.produce":
		return s.ProduceReplication(ctx, input, WaitOptions{
			Timeout:      durationSeconds(input["timeout_seconds"]),
			PollInterval: durationSeconds(input["poll_seconds"]),
			OnEvent:      runtime.OnEvent,
		})
	default:
		return nil, contract.NewError("not_found", "operation not found")
	}
}

func (s *Service) ComposeVideo(ctx context.Context, spec map[string]any, destination string, force bool, options WaitOptions) (map[string]any, error) {
	pinned, err := s.pinProjectProfiles(ctx, spec)
	if err != nil {
		return nil, composeError("profile", "", err)
	}
	created, err := jsonActionWithID(ctx, s, http.MethodPost, "/api/video-projects", pinned, "project_id", "id")
	if err != nil {
		return nil, composeError("create", "", err)
	}
	projectID := stringValue(created["id"], "")
	if options.OnEvent != nil {
		options.OnEvent(map[string]any{"type": "project_created", "project_id": projectID, "phase": "create"})
	}
	if _, err = jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(projectID)+"/run", map[string]any{}); err != nil {
		return nil, composeError("run", projectID, err)
	}
	if _, err = s.WaitProject(ctx, projectID, options.Timeout, options.PollInterval, options.OnEvent); err != nil {
		return nil, composeError("generate", projectID, err)
	}
	if _, err = jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(projectID)+"/merge", map[string]any{}); err != nil {
		return nil, composeError("merge", projectID, err)
	}
	project, err := s.WaitProject(ctx, projectID, options.Timeout, options.PollInterval, options.OnEvent)
	if err != nil {
		return nil, composeError("merge_wait", projectID, err)
	}
	download, err := s.API.Download(ctx, "/api/video-projects/"+url.PathEscape(projectID)+"/merged/download", destination, force)
	if err != nil {
		return nil, composeError("download", projectID, err)
	}
	return map[string]any{"project_id": projectID, "project": project, "download": download}, nil
}

func (s *Service) pinProjectProfiles(ctx context.Context, spec map[string]any) (map[string]any, error) {
	raw, err := json.Marshal(spec)
	if err != nil {
		return nil, contract.NewError("invalid_spec", "project spec cannot be encoded")
	}
	pinned := map[string]any{}
	if err := json.Unmarshal(raw, &pinned); err != nil {
		return nil, contract.NewError("invalid_spec", "project spec cannot be copied")
	}
	segments, _ := pinned["segments"].([]any)
	needsProfiles := false
	for _, rawSegment := range segments {
		segment, _ := rawSegment.(map[string]any)
		request, _ := segment["request"].(map[string]any)
		if request != nil && (stringValue(request["profile_version"], "") == "" || stringValue(request["profile_digest"], "") == "") {
			needsProfiles = true
		}
	}
	if !needsProfiles {
		return pinned, nil
	}
	capabilities, err := s.API.Get(ctx, "/api/capabilities")
	if err != nil {
		return nil, err
	}
	profiles, _ := capabilities["profiles"].([]any)
	byID := map[string]map[string]any{}
	for _, rawProfile := range profiles {
		profile, _ := rawProfile.(map[string]any)
		if id := stringValue(profile["id"], ""); id != "" {
			byID[id] = profile
		}
	}
	for _, rawSegment := range segments {
		segment, _ := rawSegment.(map[string]any)
		request, _ := segment["request"].(map[string]any)
		if request == nil {
			continue
		}
		id := stringValue(request["profile_id"], "")
		profile := byID[id]
		if profile == nil || profile["available"] != true {
			return nil, &contract.CLIError{Code: "profile_unavailable", Message: "project segment profile is unavailable", Details: map[string]any{"profile_id": id}}
		}
		if stringValue(request["profile_version"], "") == "" {
			request["profile_version"] = profile["version"]
		}
		if stringValue(request["profile_digest"], "") == "" {
			request["profile_digest"] = profile["manifest_sha256"]
		}
	}
	return pinned, nil
}

func composeError(phase, projectID string, err error) error {
	details := map[string]any{"phase": phase}
	if projectID != "" {
		details["project_id"] = projectID
	}
	return &contract.CLIError{
		Code:    "video_compose_failed",
		Message: "video composition failed during " + phase,
		Details: details,
		Cause:   err,
	}
}

func (s *Service) WaitProject(ctx context.Context, id string, timeout, poll time.Duration, onEvent func(map[string]any)) (map[string]any, error) {
	if !resource.ValidServerID(id) {
		return nil, contract.NewError("invalid_argument", "project_id must be 32 lowercase hex characters")
	}
	if timeout < 0 || poll < 0 {
		return nil, contract.NewError("invalid_argument", "project wait durations cannot be negative")
	}
	if poll == 0 {
		poll = s.PollInterval
	}
	if poll <= 0 {
		poll = 5 * time.Second
	}
	waitCtx := ctx
	var cancel context.CancelFunc
	if timeout > 0 {
		waitCtx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	failures := 0
	for {
		if err := waitCtx.Err(); err != nil {
			return nil, projectWaitError(id, err)
		}
		value, err := s.API.Get(waitCtx, "/api/video-projects/"+url.PathEscape(id))
		if err != nil {
			if waitErr := waitCtx.Err(); waitErr != nil {
				return nil, projectWaitError(id, waitErr)
			}
			var cliErr *contract.CLIError
			if !errors.As(err, &cliErr) || !cliErr.Retryable || failures >= 5 {
				return nil, err
			}
			failures++
			if !waitDelay(waitCtx, boundedBackoff(poll, failures)) {
				continue
			}
			continue
		}
		failures = 0
		status := stringValue(value["status"], "")
		if onEvent != nil {
			onEvent(map[string]any{"type": "project_status", "project_id": id, "status": status, "progress": value["progress"]})
		}
		switch status {
		case "completed", "merged", "partial":
			return value, nil
		case "failed":
			return nil, &contract.CLIError{Code: "job_failed", Message: "project failed", Details: value}
		case "stopped", "cancelled":
			return nil, &contract.CLIError{Code: "job_cancelled", Message: "project stopped", Details: value}
		case "draft", "running", "stopping", "merging":
		default:
			return nil, &contract.CLIError{Code: "invalid_response", Message: "server returned unknown project status", Details: map[string]any{"project_id": id, "status": status}}
		}
		waitDelay(waitCtx, poll)
	}
}

func projectWaitError(id string, err error) error {
	code, message := "interrupted", "waiting interrupted; the remote project was not stopped"
	if errors.Is(err, context.DeadlineExceeded) {
		code, message = "timeout", "timed out waiting for project; the remote project is still running"
	}
	return &contract.CLIError{Code: code, Message: message, Details: map[string]any{"project_id": id}, Cause: err}
}

func waitDelay(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func boundedBackoff(base time.Duration, failures int) time.Duration {
	delay := base * time.Duration(1<<min(failures-1, 4))
	if delay > 15*time.Second {
		return 15 * time.Second
	}
	return delay
}

func jsonAction(ctx context.Context, service *Service, method, path string, body any) (map[string]any, error) {
	value := map[string]any{}
	err := service.API.JSON(ctx, method, path, body, &value)
	return value, err
}

func jsonActionWithID(ctx context.Context, service *Service, method, path string, body any, fields ...string) (map[string]any, error) {
	value, err := jsonAction(ctx, service, method, path, body)
	if err != nil {
		return nil, err
	}
	if _, err := RequireResponseID(value, fields...); err != nil {
		return nil, err
	}
	return value, nil
}

func copyOptional(destination, source map[string]any, keys ...string) {
	for _, key := range keys {
		if value, exists := source[key]; exists {
			destination[key] = value
		}
	}
}

func voiceDownloadRoute(taskID, track string) string {
	path := "/api/voice/tasks/" + url.PathEscape(taskID) + "/download"
	if track != "mix" {
		path += "?track=" + url.QueryEscape(track)
	}
	return path
}

func boolValue(value any) bool {
	result, _ := value.(bool)
	return result
}

func intValue(value any, fallback int) int {
	if numeric, ok := value.(float64); ok {
		return int(numeric)
	}
	return fallback
}

func numberValue(value any, fallback float64) float64 {
	if numeric, ok := value.(float64); ok {
		return numeric
	}
	return fallback
}

func durationSeconds(value any) time.Duration {
	if numeric, ok := value.(float64); ok {
		return time.Duration(numeric * float64(time.Second))
	}
	return 0
}
