package operation

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"h3studio/cli/internal/api"
	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/resource"
)

type Service struct {
	API          *api.Client
	Context      string
	PollInterval time.Duration
}

type WaitOptions struct {
	Timeout      time.Duration
	PollInterval time.Duration
	OnEvent      func(map[string]any)
}

func (s *Service) Health(ctx context.Context) (map[string]any, error) {
	return s.API.Get(ctx, "/health")
}
func (s *Service) Capabilities(ctx context.Context) (map[string]any, error) {
	return s.API.Get(ctx, "/api/capabilities")
}

func (s *Service) PlanCharacterMigration(ctx context.Context, input map[string]any) (map[string]any, error) {
	raw, err := json.Marshal(input)
	if err != nil {
		return nil, contract.NewError("invalid_spec", "character migration spec cannot be encoded")
	}
	body := map[string]any{}
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, contract.NewError("invalid_spec", "character migration spec cannot be copied")
	}
	delete(body, "to")
	delete(body, "force")
	delete(body, "detach")
	delete(body, "timeout_seconds")
	delete(body, "poll_seconds")
	if err := ValidateInput("video.character_migration.plan", body); err != nil {
		return nil, err
	}
	sourceRef, sourceEvidence, err := s.ResolveAsset(ctx, stringValue(body["source"], ""), "motion")
	if err != nil {
		return nil, err
	}
	targets, _ := body["targets"].([]any)
	if len(targets) != 1 {
		return nil, contract.NewError("invalid_argument", "targets must contain exactly one character migration target in v1")
	}
	target, _ := targets[0].(map[string]any)
	characterRef, characterEvidence, err := s.ResolveAsset(ctx, stringValue(target["character"], ""), "identity")
	if err != nil {
		return nil, err
	}
	serverTarget := map[string]any{
		"character_asset_id": characterRef["asset_id"],
		"source_subject":     target["source_subject"],
	}
	copyOptional(serverTarget, target, "details")
	delete(body, "source")
	body["source_asset_id"] = sourceRef["asset_id"]
	body["targets"] = []any{serverTarget}
	value := map[string]any{}
	if err := s.API.JSON(ctx, http.MethodPost, "/api/video/character-migration/plan", body, &value); err != nil {
		return nil, err
	}
	value["resolved_resources"] = []any{sourceEvidence, characterEvidence}
	return value, nil
}

func (s *Service) ProduceCharacterMigration(ctx context.Context, input map[string]any, options WaitOptions) (map[string]any, error) {
	destination := stringValue(input["to"], "")
	force := boolValue(input["force"])
	detach := boolValue(input["detach"])
	if destination == "" && !detach {
		return nil, contract.NewError("invalid_argument", "to is required unless planning only")
	}
	if destination != "" {
		if _, err := os.Stat(destination); err == nil && !force {
			return nil, contract.NewError("output_exists", "output already exists; pass force=true or --force to replace it")
		} else if err != nil && !os.IsNotExist(err) {
			return nil, &contract.CLIError{Code: "local_file", Message: err.Error(), Cause: err}
		}
	}
	planned, err := s.PlanCharacterMigration(ctx, input)
	if err != nil {
		return nil, err
	}
	projectSpec, ok := planned["project"].(map[string]any)
	if !ok {
		return nil, invalidIDResponse("character migration plan did not contain a project")
	}
	created, err := jsonActionWithID(ctx, s, http.MethodPost, "/api/video-projects", projectSpec, "project_id", "id")
	if err != nil {
		return nil, migrationError("create", "", err)
	}
	projectID := stringValue(created["id"], "")
	if options.OnEvent != nil {
		options.OnEvent(map[string]any{"type": "project_created", "project_id": projectID, "phase": "create"})
	}
	if _, err = jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(projectID)+"/run", map[string]any{}); err != nil {
		return nil, migrationError("run", projectID, err)
	}
	if detach {
		return map[string]any{"project_id": projectID, "project": created, "plan": planned, "detached": true}, nil
	}
	if _, err = s.WaitProject(ctx, projectID, options.Timeout, options.PollInterval, options.OnEvent); err != nil {
		return nil, migrationError("generate", projectID, err)
	}
	if _, err = jsonAction(ctx, s, http.MethodPost, "/api/video-projects/"+url.PathEscape(projectID)+"/merge", map[string]any{}); err != nil {
		return nil, migrationError("merge", projectID, err)
	}
	project, err := s.WaitProject(ctx, projectID, options.Timeout, options.PollInterval, options.OnEvent)
	if err != nil {
		return nil, migrationError("merge_wait", projectID, err)
	}
	download, err := s.API.Download(ctx, "/api/video-projects/"+url.PathEscape(projectID)+"/merged/download", destination, force)
	if err != nil {
		return nil, migrationError("download", projectID, err)
	}
	return map[string]any{"project_id": projectID, "project": project, "plan": planned, "download": download}, nil
}

func migrationError(phase, projectID string, err error) error {
	details := map[string]any{"phase": phase}
	if projectID != "" {
		details["project_id"] = projectID
	}
	return &contract.CLIError{Code: "character_migration_failed", Message: "character migration failed during " + phase, Details: details, Cause: err}
}

func (s *Service) MuxAudio(ctx context.Context, video, audio string, body map[string]any) (map[string]any, error) {
	videoRef, videoEvidence, err := s.ResolveAsset(ctx, video, "video")
	if err != nil {
		return nil, err
	}
	audioRef, audioEvidence, err := s.ResolveAsset(ctx, audio, "audio")
	if err != nil {
		return nil, err
	}
	payload := map[string]any{
		"video": map[string]any{"type": "asset", "asset_id": videoRef["asset_id"]},
		"audio": map[string]any{"type": "asset", "asset_id": audioRef["asset_id"]},
	}
	copyOptional(payload, body, "duration", "display_name")
	value := map[string]any{}
	if err := s.API.JSONMedia(ctx, http.MethodPost, "/api/media/mux-audio", payload, &value); err != nil {
		return nil, err
	}
	id, err := RequireResponseID(value, "receipt_id", "id")
	if err != nil {
		return nil, err
	}
	value["locator"] = "media:" + id
	value["resolved_resources"] = []any{videoEvidence, audioEvidence}
	return value, nil
}

func (s *Service) SubmitVoice(ctx context.Context, engine, source, reference, requestID string, tuning ...map[string]any) (map[string]any, error) {
	if engine != "vevo2" && engine != "yingmusic" {
		return nil, contract.NewError("invalid_argument", "voice engine must be vevo2 or yingmusic")
	}
	if len(tuning) > 1 || (engine != "yingmusic" && len(tuning) > 0 && len(tuning[0]) > 0) {
		return nil, contract.NewError("invalid_argument", "voice tuning is supported only for yingmusic")
	}
	if requestID == "" {
		raw := make([]byte, 16)
		if _, err := rand.Read(raw); err != nil {
			return nil, &contract.CLIError{Code: "request_id_failed", Message: "could not create voice request_id", Cause: err}
		}
		requestID = hex.EncodeToString(raw)
	}
	sourceRef, sourceEvidence, err := s.ResolveAsset(ctx, source, "voice_source")
	if err != nil {
		return nil, err
	}
	referenceRef, referenceEvidence, err := s.ResolveAsset(ctx, reference, "voice_reference")
	if err != nil {
		return nil, err
	}
	body := map[string]any{
		"engine": engine, "source_asset_id": sourceRef["asset_id"],
		"reference_asset_id": referenceRef["asset_id"], "request_id": requestID,
	}
	if len(tuning) == 1 {
		for key, value := range tuning[0] {
			switch key {
			case "diffusion_steps", "inference_cfg_rate", "seed", "output_options":
				body[key] = value
			default:
				return nil, contract.NewError("invalid_argument", "unsupported voice tuning parameter")
			}
		}
	}
	value := map[string]any{}
	status, err := s.API.JSONStatus(ctx, http.MethodPost, "/api/voice/tasks", body, &value)
	if err != nil {
		return nil, err
	}
	if status != http.StatusAccepted {
		return nil, invalidIDResponse(fmt.Sprintf("voice submission returned HTTP %d instead of 202", status))
	}
	taskID, err := RequireResponseID(value, "task_id", "id")
	if err != nil {
		return nil, err
	}
	value["task_id"] = taskID
	value["request_id"] = requestID
	value["resolved_resources"] = []any{sourceEvidence, referenceEvidence}
	return value, nil
}

func (s *Service) WaitVoice(ctx context.Context, taskID string, options WaitOptions) (map[string]any, error) {
	if !resource.ValidServerID(taskID) {
		return nil, contract.NewError("invalid_argument", "voice task id must be 32 lowercase hex characters")
	}
	interval := options.PollInterval
	if interval <= 0 {
		interval = s.PollInterval
	}
	if interval <= 0 {
		interval = 5 * time.Second
	}
	waitCtx := ctx
	var cancel context.CancelFunc
	if options.Timeout > 0 {
		waitCtx, cancel = context.WithTimeout(ctx, options.Timeout)
		defer cancel()
	}
	for {
		if err := waitCtx.Err(); err != nil {
			code, message := "interrupted", "waiting interrupted; the remote voice task was not canceled"
			if errors.Is(err, context.DeadlineExceeded) {
				code, message = "timeout", "timed out waiting for voice task; it is still running"
			}
			return nil, &contract.CLIError{Code: code, Message: message, Details: map[string]any{"task_id": taskID}, Cause: err}
		}
		value, err := s.API.Get(waitCtx, "/api/voice/tasks/"+url.PathEscape(taskID))
		if err != nil {
			return nil, err
		}
		status := stringValue(value["status"], "")
		if options.OnEvent != nil {
			options.OnEvent(map[string]any{"type": "voice_status", "task_id": taskID, "status": status, "stage": value["stage"], "progress": value["progress"], "queue_reason": value["queue_reason"]})
		}
		switch status {
		case "completed":
			return value, nil
		case "failed":
			return nil, &contract.CLIError{Code: "voice_failed", Message: "voice conversion failed", Details: value}
		case "canceled", "cancelled":
			return nil, &contract.CLIError{Code: "voice_canceled", Message: "voice conversion was canceled", Details: value}
		case "queued", "running", "cancelling":
		default:
			return nil, &contract.CLIError{Code: "invalid_response", Message: "server returned unknown voice task status", Details: value}
		}
		if !waitDelay(waitCtx, interval) {
			continue
		}
	}
}

func (s *Service) Upload(ctx context.Context, path, kind string) (map[string]any, error) {
	if kind == "" {
		kind = "auto"
	}
	value, err := s.API.Upload(ctx, path, kind)
	if err != nil {
		return nil, err
	}
	if _, err := RequireResponseID(value, "asset_id", "id"); err != nil {
		return nil, err
	}
	return value, nil
}

func (s *Service) ResolveAsset(ctx context.Context, raw, role string) (map[string]any, map[string]any, error) {
	locator, err := resource.Parse(raw)
	if err != nil {
		return nil, nil, &contract.CLIError{Code: "invalid_locator", Message: err.Error(), Cause: err}
	}
	resolved := map[string]any{"input": raw, "kind": locator.Kind}
	var assetID string
	switch locator.Kind {
	case resource.Local:
		result, err := s.Upload(ctx, locator.Path, "auto")
		if err != nil {
			return nil, nil, err
		}
		assetID, _ = result["asset_id"].(string)
		resolved["uploaded"] = true
		resolved["path"] = locator.Path
	case resource.Asset:
		assetID = locator.ID
	case resource.Remote:
		if locator.Context != s.Context {
			return nil, nil, contract.Unsupported("cross-context resource resolution", "use `asset copy` before generation")
		}
		assetID = locator.ID
	case resource.Job:
		body := map[string]any{"index": locator.Index, "visibility": "internal"}
		value := map[string]any{}
		if err := s.API.JSON(ctx, http.MethodPost, "/api/jobs/"+url.PathEscape(locator.ID)+"/assets", body, &value); err != nil {
			return nil, nil, err
		}
		assetID, _ = value["asset_id"].(string)
		resolved["materialized"] = true
	case resource.Media:
		value := map[string]any{}
		if err := s.API.JSON(ctx, http.MethodPost, "/api/derivations/"+url.PathEscape(locator.ID)+"/assets", map[string]any{"visibility": "internal"}, &value); err != nil {
			return nil, nil, err
		}
		assetID, _ = value["asset_id"].(string)
		resolved["materialized"] = true
	}
	if !resource.ValidServerID(assetID) {
		return nil, nil, invalidIDResponse("asset operation returned an invalid asset_id")
	}
	resolved["asset_id"] = assetID
	reference := map[string]any{"asset_id": assetID, "role": role}
	return reference, resolved, nil
}

func (s *Service) Generate(ctx context.Context, payload map[string]any) (map[string]any, error) {
	if stringValue(payload["request_id"], "") == "" {
		raw := make([]byte, 16)
		if _, err := rand.Read(raw); err != nil {
			return nil, &contract.CLIError{Code: "request_id_failed", Message: "could not create generation request_id", Cause: err}
		}
		payload["request_id"] = hex.EncodeToString(raw)
	}
	profile, _ := payload["profile_id"].(string)
	if profile != "" && profile != "auto" && (stringValue(payload["profile_version"], "") == "" || stringValue(payload["profile_digest"], "") == "") {
		capabilities, err := s.Capabilities(ctx)
		if err != nil {
			return nil, err
		}
		profiles, _ := capabilities["profiles"].([]any)
		var selected map[string]any
		for _, item := range profiles {
			candidate, _ := item.(map[string]any)
			if candidate["id"] == profile {
				selected = candidate
				break
			}
		}
		if selected == nil {
			return nil, contract.NewError("profile_not_found", fmt.Sprintf("profile %q was not returned by /api/capabilities", profile))
		}
		payload["profile_version"] = selected["version"]
		payload["profile_digest"] = selected["manifest_sha256"]
	}
	prepared, resolved, err := s.PrepareGeneration(ctx, payload)
	if err != nil {
		return nil, err
	}
	payload = prepared
	requestID := stringValue(payload["request_id"], "")
	var last error
	for attempt := 1; attempt <= 3; attempt++ {
		value := map[string]any{}
		status, requestErr := s.API.JSONStatus(ctx, http.MethodPost, "/api/generate", payload, &value)
		if requestErr == nil {
			jobID, receiptErr := RequireResponseID(value, "job_id", "id")
			if status != http.StatusAccepted {
				receiptErr = invalidIDResponse(fmt.Sprintf("generation submission returned HTTP %d instead of 202", status))
			}
			if receiptErr == nil {
				value["job_id"] = jobID
				value["resolved_resources"] = resolved
				return value, nil
			}
			requestErr = receiptErr
		}
		{
			err := requestErr
			last = err
			var cliErr *contract.CLIError
			retryable := errors.As(err, &cliErr) && (cliErr.Retryable || cliErr.Code == "network_error" || cliErr.Code == "invalid_response")
			if !retryable {
				return nil, withRequestID(err, requestID, attempt)
			}
			if attempt == 3 {
				break
			}
			timer := time.NewTimer(time.Duration(attempt) * 100 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				last = ctx.Err()
				attempt = 3
			case <-timer.C:
			}
		}
	}
	details := map[string]any{"request_id": requestID, "attempts": 3}
	var typed *contract.CLIError
	if errors.As(last, &typed) {
		details["last_error"] = map[string]any{"code": typed.Code, "status": typed.Status, "details": typed.Details}
	}
	return nil, &contract.CLIError{Code: "submission_recovery_failed", Message: "generation submission could not be recovered safely", Retryable: true, Details: details, Cause: last}
}

func (s *Service) Resume(ctx context.Context, jobID string, additionalSteps int, requestID string) (map[string]any, error) {
	if !resource.ValidServerID(jobID) {
		return nil, contract.NewError("invalid_argument", "job_id must be 32 lowercase hex characters")
	}
	if additionalSteps <= 0 {
		return nil, contract.NewError("invalid_argument", "additional_steps must be positive")
	}
	if requestID == "" {
		raw := make([]byte, 16)
		if _, err := rand.Read(raw); err != nil {
			return nil, &contract.CLIError{Code: "request_id_failed", Message: "could not create resume request_id", Cause: err}
		}
		requestID = hex.EncodeToString(raw)
	}
	body := map[string]any{"additional_steps": additionalSteps, "request_id": requestID}
	value := map[string]any{}
	status, err := s.API.JSONStatus(ctx, http.MethodPost, "/api/jobs/"+url.PathEscape(jobID)+"/resume", body, &value)
	if err != nil {
		return nil, err
	}
	if status != http.StatusAccepted {
		return nil, invalidIDResponse(fmt.Sprintf("resume submission returned HTTP %d instead of 202", status))
	}
	if _, err := RequireResponseID(value, "job_id"); err != nil {
		return nil, err
	}
	value["request_id"] = requestID
	return value, nil
}

func withRequestID(err error, requestID string, attempts int) error {
	details := map[string]any{"request_id": requestID, "attempts": attempts}
	var typed *contract.CLIError
	if errors.As(err, &typed) {
		if existing, ok := typed.Details.(map[string]any); ok {
			for key, value := range existing {
				details[key] = value
			}
		}
		clone := *typed
		clone.Details = details
		return &clone
	}
	return &contract.CLIError{Code: "submission_failed", Message: err.Error(), Details: details, Cause: err}
}

// PrepareGeneration is the single resource/prompt-mode normalization path used
// by typed commands, --spec, and operation run.
func (s *Service) PrepareGeneration(ctx context.Context, input map[string]any) (map[string]any, []any, error) {
	raw, err := json.Marshal(input)
	if err != nil {
		return nil, nil, err
	}
	payload := map[string]any{}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, nil, err
	}
	kind := stringValue(payload["output_type"], stringValue(payload["type"], ""))
	if kind != "image" && kind != "video" {
		return nil, nil, contract.NewError("invalid_argument", "output_type must be image or video")
	}
	if parameters, ok := payload["parameters"].(map[string]any); ok && kind == "video" {
		for _, key := range []string{"director_mode", "source_asset_id"} {
			if _, direct := payload[key]; !direct {
				if value, nested := parameters[key]; nested {
					payload[key] = value
				}
			}
			delete(parameters, key)
		}
	}
	if kind == "video" {
		if _, exists := payload["negative_prompt"]; exists {
			return nil, nil, contract.NewError("invalid_argument", "negative_prompt is only valid for image generation")
		}
		mode, exists := payload["prompt_mode"]
		if !exists {
			payload["prompt_mode"] = "preserve_tags_only"
		} else if mode != "default" && mode != "preserve_tags_only" {
			return nil, nil, contract.NewError("invalid_argument", "prompt_mode must be default or preserve_tags_only")
		}
	} else {
		for _, key := range []string{"prompt_mode", "director_mode", "source_asset_id"} {
			if _, exists := payload[key]; exists {
				return nil, nil, contract.NewError("invalid_argument", key+" is only valid for video generation")
			}
		}
	}
	if err := ValidateGenerationPayload(kind, payload); err != nil {
		return nil, nil, err
	}
	if parameters, ok := payload["parameters"].(map[string]any); ok {
		if kind == "image" {
			if _, exists := parameters["duration"]; exists {
				return nil, nil, contract.NewError("invalid_argument", "parameters.duration is only valid for video generation")
			}
		} else if _, exists := parameters["cfg"]; exists {
			return nil, nil, contract.NewError("invalid_argument", "parameters.cfg is only valid for image generation")
		}
		for _, key := range []string{"duration", "denoise", "cfg"} {
			if raw, exists := parameters[key]; exists {
				number, ok := raw.(float64)
				if !ok || number <= 0 || math.IsNaN(number) || math.IsInf(number, 0) {
					return nil, nil, contract.NewError("invalid_argument", "parameters."+key+" must be a positive finite number")
				}
			}
		}
	}
	rawRefs, exists := payload["references"]
	if !exists {
		rawRefs = []any{}
	}
	refs, ok := rawRefs.([]any)
	if !ok {
		return nil, nil, contract.NewError("invalid_argument", "references must be an array")
	}
	endpointRoles := map[string]bool{}
	for index, rawRef := range refs {
		item, ok := rawRef.(map[string]any)
		if !ok {
			continue
		}
		role := stringValue(item["role"], "reference")
		if role == "first_frame" || role == "last_frame" {
			if endpointRoles[role] {
				return nil, nil, contract.NewError("invalid_argument", fmt.Sprintf("reference %d repeats %s", index, role))
			}
			endpointRoles[role] = true
		}
	}
	if kind == "video" {
		if err := validateVideoShape(payload, refs); err != nil {
			return nil, nil, err
		}
	}
	resolvedRefs, resolved := make([]any, 0, len(refs)), []any{}
	resolvedSources := map[string]string{}
	for index, rawRef := range refs {
		item := map[string]any{}
		switch value := rawRef.(type) {
		case string:
			item["source"] = value
			item["role"] = "reference"
		case map[string]any:
			for key, val := range value {
				item[key] = val
			}
		default:
			return nil, nil, contract.NewError("invalid_argument", fmt.Sprintf("reference %d must be a string or object", index))
		}
		role := stringValue(item["role"], "reference")
		source := stringValue(item["source"], "")
		if source == "" {
			asset := stringValue(item["asset_id"], "")
			if asset == "" {
				return nil, nil, contract.NewError("invalid_argument", fmt.Sprintf("reference %d requires source or asset_id", index))
			}
			if strings.Contains(asset, ":") || strings.ContainsAny(asset, "/\\") {
				source = asset
			}
		}
		if source != "" {
			var reference map[string]any
			if assetID := resolvedSources[source]; assetID != "" {
				reference = map[string]any{"asset_id": assetID, "role": role}
			} else {
				var evidence map[string]any
				reference, evidence, err = s.ResolveAsset(ctx, source, role)
				if err != nil {
					return nil, nil, err
				}
				resolvedSources[source] = stringValue(reference["asset_id"], "")
				resolved = append(resolved, evidence)
			}
			for key, val := range item {
				if key != "source" && key != "asset_id" && key != "role" {
					reference[key] = val
				}
			}
			item = reference
		} else if !resource.ValidServerID(stringValue(item["asset_id"], "")) {
			return nil, nil, contract.NewError("invalid_argument", fmt.Sprintf("reference %d asset_id must be 32 lowercase hex characters", index))
		}
		item["role"] = role
		if _, exists := item["reference_index"]; !exists {
			item["reference_index"] = index
		}
		resolvedRefs = append(resolvedRefs, item)
	}
	payload["references"] = resolvedRefs
	if source := stringValue(payload["source_asset_id"], ""); source != "" && locatorLike(source) {
		if assetID := resolvedSources[source]; assetID != "" {
			payload["source_asset_id"] = assetID
		} else {
			reference, evidence, err := s.ResolveAsset(ctx, source, "motion")
			if err != nil {
				return nil, nil, err
			}
			payload["source_asset_id"] = reference["asset_id"]
			resolved = append(resolved, evidence)
		}
	}
	if source := stringValue(payload["source_asset_id"], ""); source != "" {
		if !resource.ValidServerID(source) {
			return nil, nil, contract.NewError("invalid_argument", "source_asset_id must resolve to 32 lowercase hex characters")
		}
		sourceReference := map[string]any(nil)
		others := make([]any, 0, len(resolvedRefs))
		for _, raw := range resolvedRefs {
			item, _ := raw.(map[string]any)
			if stringValue(item["asset_id"], "") == source {
				if sourceReference == nil {
					sourceReference = item
				}
				continue
			}
			others = append(others, item)
		}
		if sourceReference == nil {
			sourceReference = map[string]any{"asset_id": source}
		}
		sourceReference["role"] = "motion"
		resolvedRefs = append([]any{sourceReference}, others...)
		if len(resolvedRefs) > 12 {
			return nil, nil, contract.NewError("invalid_argument", "H3 accepts at most 12 total reference files including source_asset_id")
		}
		for index, raw := range resolvedRefs {
			raw.(map[string]any)["reference_index"] = index
		}
		payload["references"] = resolvedRefs
	}
	return payload, resolved, nil
}

func validateVideoShape(payload map[string]any, refs []any) error {
	mode := stringValue(payload["director_mode"], "")
	if mode != "t2v" && mode != "i2v" && mode != "fl2v" && mode != "r2v" && mode != "v2v" && mode != "rv2v" {
		return contract.NewError("invalid_argument", "director_mode must be t2v, i2v, fl2v, r2v, v2v, or rv2v")
	}
	if len(refs) > 12 {
		return contract.NewError("invalid_argument", "H3 accepts at most 12 total reference files")
	}
	source := stringValue(payload["source_asset_id"], "")
	first, last, other := 0, 0, 0
	for _, raw := range refs {
		item, _ := raw.(map[string]any)
		role := stringValue(item["role"], "reference")
		itemSource := stringValue(item["source"], stringValue(item["asset_id"], ""))
		if source != "" && itemSource == source {
			continue
		}
		switch role {
		case "first_frame":
			first++
		case "last_frame":
			last++
		default:
			other++
		}
	}
	meaningful := first + last + other
	var message string
	switch mode {
	case "t2v":
		if meaningful != 0 || source != "" {
			message = "t2v does not accept references or source_asset_id"
		}
	case "i2v":
		if first != 1 || last != 0 || other != 0 || source != "" {
			message = "i2v requires exactly one first_frame reference"
		}
	case "fl2v":
		if first != 1 || last > 1 || other != 0 || source != "" {
			message = "fl2v requires a first_frame and optional last_frame only"
		}
	case "r2v":
		if meaningful == 0 || source != "" {
			message = "r2v requires references and no source_asset_id"
		}
	case "v2v":
		if source == "" || meaningful != 0 {
			message = "v2v requires exactly source_asset_id"
		}
	case "rv2v":
		if source == "" || meaningful == 0 {
			message = "rv2v requires source_asset_id plus references"
		}
	}
	if message != "" {
		return contract.NewError("invalid_argument", message)
	}
	return nil
}

func locatorLike(value string) bool {
	if strings.Contains(value, ":") || strings.ContainsAny(value, "/\\") {
		return true
	}
	// A bare server asset ID and a bare local filename are both legal strings.
	// Existing filesystem entries are unambiguously local; non-existing bare
	// values remain server IDs and are validated there.
	_, err := os.Lstat(value)
	return err == nil
}

func (s *Service) Wait(ctx context.Context, jobID string, options WaitOptions) (map[string]any, error) {
	if !resource.ValidServerID(jobID) {
		return nil, contract.NewError("invalid_argument", "job_id must be 32 lowercase hex characters")
	}
	if options.Timeout < 0 {
		return nil, contract.NewError("invalid_argument", "wait timeout cannot be negative")
	}
	if options.PollInterval < 0 {
		return nil, contract.NewError("invalid_argument", "poll interval cannot be negative")
	}
	interval := options.PollInterval
	if interval <= 0 {
		interval = s.PollInterval
	}
	if interval <= 0 {
		interval = 5 * time.Second
	}
	waitCtx := ctx
	var cancel context.CancelFunc
	if options.Timeout > 0 {
		waitCtx, cancel = context.WithTimeout(ctx, options.Timeout)
		defer cancel()
	}
	consecutiveFailures := 0
	for {
		select {
		case <-waitCtx.Done():
			code, message := "interrupted", "waiting interrupted; the remote job was not cancelled"
			if waitCtx.Err() == context.DeadlineExceeded {
				code, message = "timeout", "timed out waiting for job; the remote job is still running"
			}
			return nil, &contract.CLIError{Code: code, Message: message, Details: map[string]any{"job_id": jobID}, Cause: waitCtx.Err()}
		default:
		}
		value, err := s.API.Get(waitCtx, "/api/status?id="+url.QueryEscape(jobID))
		if err != nil {
			var cliErr *contract.CLIError
			if !errors.As(err, &cliErr) || !cliErr.Retryable || consecutiveFailures >= 5 {
				return nil, err
			}
			consecutiveFailures++
			if options.OnEvent != nil {
				options.OnEvent(map[string]any{"type": "retry", "attempt": consecutiveFailures, "reason": cliErr.Code})
			}
			delay := interval * time.Duration(1<<min(consecutiveFailures-1, 4))
			if delay > 15*time.Second {
				delay = 15 * time.Second
			}
			timer := time.NewTimer(delay)
			select {
			case <-waitCtx.Done():
				timer.Stop()
				continue
			case <-timer.C:
			}
			continue
		} else {
			consecutiveFailures = 0
			status, _ := value["status"].(string)
			if options.OnEvent != nil {
				options.OnEvent(map[string]any{"type": "status", "job_id": jobID, "status": status, "progress": value["progress"]})
			}
			switch status {
			case "completed":
				return value, nil
			case "failed":
				return nil, &contract.CLIError{Code: "job_failed", Message: stringValue(value["message"], "generation failed"), Details: value}
			case "cancelled", "canceled":
				return nil, &contract.CLIError{Code: "job_cancelled", Message: "generation was cancelled", Details: value}
			case "submitting", "queued", "running":
			default:
				return nil, &contract.CLIError{Code: "invalid_response", Message: "server returned unknown job status", Details: map[string]any{"job_id": jobID, "status": status}}
			}
		}
		timer := time.NewTimer(interval)
		select {
		case <-waitCtx.Done():
			timer.Stop()
			continue
		case <-timer.C:
		}
	}
}

func (s *Service) Derive(ctx context.Context, source string, body map[string]any) (map[string]any, error) {
	return s.DeriveWithEvents(ctx, source, body, nil)
}

func (s *Service) DeriveWithEvents(ctx context.Context, source string, body map[string]any, onEvent func(map[string]any)) (map[string]any, error) {
	locator, err := resource.Parse(source)
	if err != nil {
		return nil, &contract.CLIError{Code: "invalid_locator", Message: err.Error(), Cause: err}
	}
	if locator.Kind == resource.Local {
		upload, err := s.Upload(ctx, locator.Path, "auto")
		if err != nil {
			return nil, err
		}
		locator.Kind, locator.ID = resource.Asset, stringValue(upload["asset_id"], "")
	}
	if locator.Kind == resource.Remote && locator.Context != s.Context {
		return nil, contract.Unsupported("cross-context media input", "copy the asset to the active context first")
	}
	body["source"] = locator.DeriveSource()
	value := map[string]any{}
	if stringValue(body["operation"], "") != "prepare_h3_reference" {
		if err := s.API.JSONMedia(ctx, http.MethodPost, "/api/media/derive", body, &value); err != nil {
			return nil, err
		}
		if _, err := RequireResponseID(value, "receipt_id", "id"); err != nil {
			return nil, err
		}
		return value, nil
	}
	body["background"] = true
	status, err := s.API.JSONStatus(ctx, http.MethodPost, "/api/media/derive", body, &value)
	if err != nil {
		return nil, err
	}
	if status == http.StatusOK || status == http.StatusCreated {
		if _, err := RequireResponseID(value, "receipt_id", "id"); err != nil {
			return nil, err
		}
		return value, nil
	}
	if status != http.StatusAccepted {
		return nil, invalidIDResponse(fmt.Sprintf("reference preprocessing returned HTTP %d instead of 202", status))
	}
	taskID, err := RequireResponseID(value, "task_id", "id")
	if err != nil {
		return nil, err
	}
	if onEvent != nil {
		onEvent(map[string]any{"type": "media_submitted", "task_id": taskID, "status": value["status"], "progress": value["progress"]})
	}
	cancelRemote := func() error {
		cancelCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return s.API.JSON(cancelCtx, http.MethodPost, "/api/media-tasks/"+url.PathEscape(taskID)+"/cancel", map[string]any{}, nil)
	}
	cancelledError := func(cause error) error {
		cancelErr := cancelRemote()
		details := map[string]any{"task_id": taskID}
		if cancelErr != nil {
			details["cancel_error"] = cancelErr.Error()
		}
		return &contract.CLIError{Code: "cancelled", Message: "reference preprocessing was cancelled", Details: details, Cause: cause}
	}
	interval := s.PollInterval
	if interval <= 0 || interval > time.Second {
		interval = time.Second
	}
	for {
		select {
		case <-ctx.Done():
			return nil, cancelledError(ctx.Err())
		default:
		}
		task, err := s.API.Get(ctx, "/api/media-tasks/"+url.PathEscape(taskID))
		if err != nil {
			if ctx.Err() != nil {
				return nil, cancelledError(ctx.Err())
			}
			return nil, err
		}
		state := stringValue(task["status"], "")
		if onEvent != nil {
			onEvent(map[string]any{"type": "media_progress", "task_id": taskID, "status": state, "progress": task["progress"]})
		}
		switch state {
		case "completed":
			receipt, ok := task["receipt"].(map[string]any)
			if !ok {
				return nil, invalidIDResponse("completed media task did not contain a receipt")
			}
			if _, err := RequireResponseID(receipt, "receipt_id", "id"); err != nil {
				return nil, err
			}
			receipt["media_task_id"] = taskID
			return receipt, nil
		case "failed", "canceled", "cancelled":
			failure, _ := task["error"].(map[string]any)
			code := stringValue(failure["code"], "media_processing_failed")
			message := stringValue(failure["message"], "reference preprocessing failed")
			return nil, &contract.CLIError{Code: code, Message: message, Retryable: boolValue(failure["retryable"]), Details: map[string]any{"task_id": taskID, "task": task}}
		case "queued", "running", "cancelling":
		default:
			return nil, &contract.CLIError{Code: "invalid_response", Message: "server returned unknown media task status", Details: task}
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			continue
		case <-timer.C:
		}
	}
}

// RequireResponseID validates creation/materialization receipts before their
// identifiers can be used by another request.
func RequireResponseID(value map[string]any, fields ...string) (string, error) {
	selected := ""
	for _, field := range fields {
		raw, exists := value[field]
		if !exists {
			continue
		}
		id, ok := raw.(string)
		if !ok || !resource.ValidServerID(id) {
			return "", invalidIDResponse("server response contained an invalid " + field)
		}
		if selected != "" && selected != id {
			return "", invalidIDResponse("server response contained conflicting identifiers")
		}
		selected = id
	}
	if selected != "" {
		return selected, nil
	}
	return "", invalidIDResponse("server response did not contain a valid 32-character lowercase hex id")
}

func invalidIDResponse(message string) error {
	return &contract.CLIError{Code: "invalid_response", Message: message}
}

func ReadSpec(path string) (map[string]any, error) {
	var raw []byte
	var err error
	if path == "-" {
		raw, err = io.ReadAll(os.Stdin)
	} else {
		raw, err = os.ReadFile(path)
	}
	if err != nil {
		return nil, err
	}
	value := map[string]any{}
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil, &contract.CLIError{Code: "invalid_spec", Message: err.Error(), Cause: err}
	}
	return value, nil
}

func stringValue(value any, fallback string) string {
	if text, ok := value.(string); ok && strings.TrimSpace(text) != "" {
		return text
	}
	return fallback
}
