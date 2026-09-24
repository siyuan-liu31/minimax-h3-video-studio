package operation

import (
	"context"
	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/resource"
	"net/http"
	"net/url"
	"os"
)

// ReplicationSpec accepts a plan, a saved project or a CLI JSON envelope.
// Runtime results are deliberately omitted; importing creates an unexecuted draft.
func ReplicationSpec(value map[string]any) (map[string]any, error) {
	if data, ok := value["data"].(map[string]any); ok {
		value = data
	}
	if project, ok := value["project"].(map[string]any); ok {
		value = project
	}
	recipe, ok := value["recipe"].(map[string]any)
	if !ok || recipe["type"] != "replication" || recipe["version"] != "h3.replication/v1" {
		return nil, contract.NewError("not_replication_project", "expected a versioned replication plan or project")
	}
	segments, ok := value["segments"].([]any)
	if !ok || len(segments) == 0 {
		return nil, contract.NewError("invalid_spec", "replication project requires segments")
	}
	result := map[string]any{"title": value["title"], "recipe": recipe}
	if board, ok := value["storyboard"]; ok {
		result["storyboard"] = board
	}
	clean := make([]any, 0, len(segments))
	for _, raw := range segments {
		segment, ok := raw.(map[string]any)
		if !ok {
			return nil, contract.NewError("invalid_spec", "invalid replication segment")
		}
		item := map[string]any{}
		copyOptional(item, segment, "id", "kind", "continuation", "request", "source_range", "continuation_range", "motion_context", "media_source")
		clean = append(clean, item)
	}
	result["segments"] = clean
	return result, nil
}

func (s *Service) GetReplication(ctx context.Context, id string) (map[string]any, error) {
	if !resource.ValidServerID(id) {
		return nil, contract.NewError("invalid_argument", "project_id must be 32 lowercase hex characters")
	}
	value, err := s.API.Get(ctx, "/api/video-projects/"+url.PathEscape(id))
	if err != nil {
		return nil, err
	}
	if _, err = ReplicationSpec(value); err != nil {
		return nil, err
	}
	return value, nil
}

func (s *Service) CreateReplication(ctx context.Context, value map[string]any) (map[string]any, error) {
	spec, err := ReplicationSpec(value)
	if err != nil {
		return nil, err
	}
	return jsonActionWithID(ctx, s, http.MethodPost, "/api/video-projects", spec, "project_id", "id")
}

// FinishReplication never creates a new project or reruns an accepted segment.
func (s *Service) FinishReplication(ctx context.Context, id, destination string, force bool, options WaitOptions) (map[string]any, error) {
	if destination == "" {
		return nil, contract.NewError("invalid_argument", "to is required")
	}
	if _, err := os.Stat(destination); err == nil && !force {
		return nil, contract.NewError("output_exists", "output already exists; pass --force to replace it")
	} else if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	project, err := s.GetReplication(ctx, id)
	if err != nil {
		return nil, err
	}
	route := "/api/video-projects/" + url.PathEscape(id)
	merged, _ := project["merged"].(map[string]any)
	if merged["status"] != "completed" {
		status := stringValue(project["status"], "")
		if status == "stopping" {
			return nil, contract.NewError("project_busy", "wait for the project to stop before resuming")
		}
		if status != "running" && status != "merging" {
			if _, err = jsonAction(ctx, s, http.MethodPost, route+"/run", map[string]any{}); err != nil {
				return nil, replicationError("run", id, err)
			}
		}
		project, err = s.WaitProject(ctx, id, options.Timeout, options.PollInterval, options.OnEvent)
		if err != nil {
			return nil, replicationError("generate", id, err)
		}
		// An already-running selected-segment job may finish as partial. Once
		// it releases the project, resume the rest before attempting a merge.
		if project["status"] == "partial" {
			if _, err = jsonAction(ctx, s, http.MethodPost, route+"/run", map[string]any{}); err != nil {
				return nil, replicationError("run", id, err)
			}
			project, err = s.WaitProject(ctx, id, options.Timeout, options.PollInterval, options.OnEvent)
			if err != nil {
				return nil, replicationError("generate", id, err)
			}
		}
		merged, _ = project["merged"].(map[string]any)
		if merged["status"] != "completed" {
			if _, err = jsonAction(ctx, s, http.MethodPost, route+"/merge", map[string]any{}); err != nil {
				return nil, replicationError("merge", id, err)
			}
			project, err = s.WaitProject(ctx, id, options.Timeout, options.PollInterval, options.OnEvent)
			if err != nil {
				return nil, replicationError("merge_wait", id, err)
			}
		}
	}
	downloaded, err := s.API.Download(ctx, route+"/merged/download", destination, force)
	if err != nil {
		return nil, replicationError("download", id, err)
	}
	return map[string]any{"project_id": id, "project": project, "download": downloaded}, nil
}
