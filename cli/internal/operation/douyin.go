package operation

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/resource"
	"net/http"
	"time"
)

func (s *Service) SubmitDouyin(ctx context.Context, input map[string]any) (map[string]any, error) {
	body := map[string]any{}
	for k, v := range input {
		body[k] = v
	}
	if stringValue(body["request_id"], "") == "" {
		raw := make([]byte, 16)
		if _, err := rand.Read(raw); err != nil {
			return nil, err
		}
		body["request_id"] = hex.EncodeToString(raw)
	}
	value, err := jsonActionWithID(ctx, s, http.MethodPost, "/api/douyin/tasks", body, "task_id", "id")
	if err != nil {
		return nil, &contract.CLIError{Code: "douyin_submit_failed", Message: err.Error(), Details: map[string]any{"request_id": body["request_id"]}, Cause: err}
	}
	return value, nil
}

func (s *Service) WaitDouyin(ctx context.Context, id string, options WaitOptions) (map[string]any, error) {
	if !resource.ValidServerID(id) {
		return nil, contract.NewError("invalid_argument", "invalid Douyin task id")
	}
	waitCtx := ctx
	if options.Timeout > 0 {
		var cancel context.CancelFunc
		waitCtx, cancel = context.WithTimeout(ctx, options.Timeout)
		defer cancel()
	}
	interval := options.PollInterval
	if interval <= 0 {
		interval = 2 * time.Second
	}
	for {
		if err := waitCtx.Err(); err != nil {
			return nil, &contract.CLIError{Code: "interrupted", Message: "Waiting ended; the remote download was not canceled", Details: map[string]any{"task_id": id}, Cause: err}
		}
		task, err := s.API.Get(waitCtx, "/api/douyin/tasks/"+id)
		if err != nil {
			return nil, err
		}
		status := stringValue(task["status"], "")
		if options.OnEvent != nil {
			options.OnEvent(map[string]any{"type": "douyin_status", "task_id": id, "status": status, "progress": task["progress"], "stage": task["stage"]})
		}
		switch status {
		case "completed":
			if task["mode"] == "download" && !resource.ValidServerID(stringValue(task["asset_id"], "")) {
				return nil, contract.NewError("invalid_response", "download receipt has no valid asset id")
			}
			return task, nil
		case "failed", "canceled":
			return nil, &contract.CLIError{Code: "douyin_" + status, Message: "Douyin task " + status, Details: task}
		case "queued", "running", "cancelling":
		default:
			return nil, contract.NewError("invalid_response", "Unknown download task status")
		}
		waitDelay(waitCtx, interval)
	}
}
