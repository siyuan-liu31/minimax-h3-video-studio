package command

import (
	"context"
	"flag"
	"fmt"
	"h3studio/cli/internal/operation"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

const ReplicationHelp = `Usage: h3ctl replication COMMAND

  plan --source VIDEO --brief TEXT [video replicate planning flags]
  create --plan PATH|-                 Save a reviewed plan as a draft; no generation
  list | inspect PROJECT             Read replication projects
  export PROJECT                     Emit reusable project spec (use --json)
  edit-segment PROJECT --segment ID --prompt-file PATH [--steps N] [--seed N]
               [--expected-updated-at SECONDS]
  run PROJECT [--segment ID ...]      Start unfinished segments in the background
  wait PROJECT [--timeout 0] [--poll-interval 5s]
  stop PROJECT | rerun PROJECT --segment ID
  merge PROJECT | download PROJECT --to PATH [--force]
  resume PROJECT --to PATH [--force] [--timeout 0] [--poll-interval 5s]

plan resolves/uploads resources but never submits generation. It emits a versioned
plan; create accepts that plan, an exported spec, or its h3ctl JSON envelope.
resume uses the existing project, keeps completed segments, waits, merges if
needed and downloads. Ctrl-C stops only the local wait. run/rerun submit work.
Editing a segment invalidates its output and dependent continuation segments;
unrelated completed segments stay reusable. A stale expected-updated-at returns
project_changed. Scene analysis is cut detection, not semantic video understanding.

Examples:
  h3ctl replication plan --source source.mp4 --brief "Keep the motion" --json > plan.json
  h3ctl replication create --plan plan.json --json
  h3ctl replication inspect PROJECT --json
  h3ctl replication edit-segment PROJECT --segment SEGMENT --prompt-file shot.txt
  h3ctl replication resume PROJECT --to final.mp4
`

func (r *Runner) runReplication(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, ReplicationHelp)
		return nil, nil
	}
	action := args[0]
	if action == "plan" {
		for _, arg := range args[1:] {
			if !strings.HasPrefix(arg, "-") {
				continue
			}
			name := strings.TrimLeft(strings.SplitN(arg, "=", 2)[0], "-")
			if name == "plan-only" || name == "detach" || name == "to" {
				return nil, usage("replication plan does not accept %s; use replication resume to generate", name)
			}
		}
		return r.runVideo(ctx, append([]string{"replicate", "--plan-only"}, args[1:]...))
	}
	if action == "create" {
		set := newFlags("replication create")
		path := set.String("plan", "", "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 || *path == "" {
			return nil, usage("replication create requires --plan PATH|-")
		}
		value, err := readJSONInput(r.Streams.In, *path)
		if err != nil {
			return nil, err
		}
		return r.Service.CreateReplication(ctx, value)
	}
	if action == "list" {
		if len(args) != 1 {
			return nil, usage("replication list accepts no arguments")
		}
		value, err := r.Service.API.Get(ctx, "/api/video-projects")
		if err != nil {
			return nil, err
		}
		projects := []any{}
		items, _ := value["projects"].([]any)
		for _, raw := range items {
			if item, ok := raw.(map[string]any); ok {
				if _, err := operation.ReplicationSpec(item); err == nil {
					projects = append(projects, item)
				}
			}
		}
		return map[string]any{"projects": projects}, nil
	}
	if action == "inspect" || action == "export" {
		if len(args) != 2 {
			return nil, usage("replication %s requires PROJECT", action)
		}
		id, err := projectID(args[1])
		if err != nil {
			return nil, err
		}
		value, err := r.Service.GetReplication(ctx, id)
		if err != nil {
			return nil, err
		}
		if action == "export" {
			return operation.ReplicationSpec(value)
		}
		return value, nil
	}
	if action == "edit-segment" {
		set := newFlags("replication edit-segment")
		segment := set.String("segment", "", "")
		prompt := set.String("prompt-file", "", "")
		steps := set.Int("steps", 0, "")
		seed := set.Int64("seed", -1, "")
		expected := set.Float64("expected-updated-at", 0, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		visited := map[string]bool{}
		set.Visit(func(f *flag.Flag) { visited[f.Name] = true })
		if set.NArg() != 1 || *segment == "" || (*prompt == "" && !visited["steps"] && !visited["seed"]) {
			return nil, usage("edit-segment requires PROJECT --segment ID and prompt-file, steps or seed")
		}
		id, err := projectID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		sid, err := projectID(*segment)
		if err != nil {
			return nil, err
		}
		body := map[string]any{}
		if *prompt != "" {
			raw, err := os.ReadFile(*prompt)
			if err != nil {
				return nil, err
			}
			body["prompt"] = string(raw)
		}
		if visited["steps"] {
			body["steps"] = *steps
		}
		if visited["seed"] {
			body["seed"] = *seed
		}
		value, err := r.Service.GetReplication(ctx, id)
		if err != nil {
			return nil, err
		}
		body["expected_updated_at"] = value["updated_at"]
		if visited["expected-updated-at"] {
			body["expected_updated_at"] = *expected
		}
		result := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPatch, "/api/video-projects/"+url.PathEscape(id)+"/segments/"+url.PathEscape(sid), body, &result)
		return result, err
	}
	if action == "resume" {
		set := newFlags("replication resume")
		to := set.String("to", "", "")
		force := set.Bool("force", false, "")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 5*time.Second, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 || *to == "" || *timeout < 0 || *poll < 0 {
			return nil, usage("resume requires PROJECT --to PATH and non-negative wait durations")
		}
		id, err := projectID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		return r.Service.FinishReplication(ctx, id, *to, *force, operation.WaitOptions{Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event})
	}
	switch action {
	case "run", "wait", "stop", "rerun", "merge", "download":
		// The shared project commands own segment selection, waits and downloads.
		return r.runProject(ctx, args)
	default:
		return nil, usage("unknown replication command %q", action)
	}
}
