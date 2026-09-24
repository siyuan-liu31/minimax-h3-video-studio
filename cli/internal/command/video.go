package command

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/operation"
)

const VideoHelp = `Usage: h3ctl video COMMAND

  h3ctl video compose --spec PATH|- --to PATH [--force] [--timeout 0] [--poll-interval 5s]
  h3ctl video replicate --source VIDEO --brief TEXT [--reference IMAGE] [options]
  h3ctl video replicate --spec PATH|- [--to PATH] [--plan-only|--detach]
  h3ctl video migrate-character --source VIDEO --character IMAGE --source-subject TEXT [options]
  h3ctl video migrate-character --spec PATH|- [--to PATH] [--plan-only|--detach]
  h3ctl video trim SOURCE --start SECONDS --end SECONDS [media trim flags]
  h3ctl video concat PROJECT

compose is the end-to-end long-video command: it pins missing Profile version
metadata, creates the durable project, generates every segment in order, waits,
applies Motion Context head trim inside each chained render, concatenates the
validated 24fps outputs, and atomically downloads the final movie.

Turbo4 is a sampling preset, not a fixed step count. Set each segment's
request.parameters.steps within the selected Profile range. Motion Context uses:
  "continuation": "motion_context",
  "motion_context": {"video_frames": 22, "audio_frames": 24}

Ctrl-C only interrupts the local wait. The returned project_id can be resumed
with h3ctl project get/run/wait/merge/download.

Example:
  h3ctl video compose --spec trilogy.json --to final.mp4 --timeout 0
  h3ctl video replicate --source source.mp4 --brief "keep motion; replace product" \
    --reference product.png --to replicated.mp4
  h3ctl video migrate-character --source performance.mp4 --character hero.png \
    --source-subject "the center performer" --steps 4 --to migrated.mp4
`

func (r *Runner) runVideo(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, VideoHelp)
		return nil, nil
	}
	switch args[0] {
	case "compose":
		set := newFlags("video compose")
		specPath := set.String("spec", "", "")
		to := set.String("to", "", "")
		force := set.Bool("force", false, "")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 5*time.Second, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 || *specPath == "" || *to == "" {
			return nil, usage("video compose requires --spec PATH|- --to PATH")
		}
		if *timeout < 0 || *poll < 0 {
			return nil, usage("timeout and poll-interval cannot be negative")
		}
		spec, err := readJSONInput(r.Streams.In, *specPath)
		if err != nil {
			return nil, err
		}
		return r.Service.ComposeVideo(ctx, spec, *to, *force, operation.WaitOptions{
			Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event,
		})
	case "replicate":
		set := newFlags("video replicate")
		specPath := set.String("spec", "", "")
		source := set.String("source", "", "")
		brief := set.String("brief", "", "")
		briefFile := set.String("brief-file", "", "")
		title := set.String("title", "Replication workshop", "")
		preserve := set.String("preserve", "timing,motion,camera,composition", "")
		subject := set.String("replace-subject", "", "")
		product := set.String("replace-product", "", "")
		setting := set.String("replace-setting", "", "")
		style := set.String("style", "", "")
		profile := set.String("profile", "minimax-h3-ref2va", "")
		steps := set.Int("steps", 0, "")
		loraStrength := set.Float64("lora-strength", -1, "")
		seed := set.Int("seed", -1, "")
		continuity := set.String("continuity", "auto", "")
		audioPolicy := set.String("audio", "copy-source", "")
		promptFile := set.String("prompt-file", "", "")
		to := set.String("to", "", "")
		detach := set.Bool("detach", false, "")
		planOnly := set.Bool("plan-only", false, "")
		force := set.Bool("force", false, "")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 5*time.Second, "")
		var references stringsFlag
		set.Var(&references, "reference", "")
		if err := parseFlags(set, args[1:], "reference"); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 || *timeout < 0 || *poll < 0 {
			return nil, usage("video replicate accepts flags only; timeout and poll-interval cannot be negative")
		}
		if *detach && *planOnly {
			return nil, usage("--detach and --plan-only cannot be combined")
		}
		if !*planOnly && !*detach && *to == "" {
			return nil, usage("video replicate requires --to unless --plan-only or --detach is used")
		}
		visited := map[string]bool{}
		set.Visit(func(item *flag.Flag) { visited[item.Name] = true })
		if visited["steps"] && (*steps < 4 || *steps > 50) {
			return nil, usage("--steps must be between 4 and 50")
		}
		if visited["lora-strength"] && (*loraStrength < 0 || *loraStrength > 2) {
			return nil, usage("--lora-strength must be between 0 and 2")
		}
		if *seed < -1 {
			return nil, usage("--seed must be -1 or non-negative")
		}
		if *continuity != "auto" && *continuity != "none" && *continuity != "motion_context" {
			return nil, usage("--continuity must be auto, none, or motion_context")
		}
		if *audioPolicy != "copy-source" && *audioPolicy != "reference-source" && *audioPolicy != "generate" && *audioPolicy != "mute" {
			return nil, usage("--audio must be copy-source, reference-source, generate, or mute")
		}
		var input map[string]any
		if *specPath != "" {
			for _, name := range []string{"source", "brief", "brief-file", "title", "preserve", "replace-subject", "replace-product", "replace-setting", "style", "reference", "profile", "steps", "lora-strength", "seed", "continuity", "audio", "prompt-file"} {
				if visited[name] {
					return nil, usage("--spec cannot be combined with --%s", name)
				}
			}
			var err error
			input, err = readJSONInput(r.Streams.In, *specPath)
			if err != nil {
				return nil, err
			}
		} else {
			if *source == "" || (strings.TrimSpace(*brief) == "" && *briefFile == "") {
				return nil, usage("video replicate requires --source and --brief or --brief-file")
			}
			if strings.TrimSpace(*brief) != "" && *briefFile != "" {
				return nil, usage("--brief and --brief-file are mutually exclusive")
			}
			briefValue := strings.TrimSpace(*brief)
			if *briefFile != "" {
				raw, err := os.ReadFile(*briefFile)
				if err != nil {
					return nil, &contract.CLIError{Code: "local_file", Message: err.Error(), Cause: err}
				}
				briefValue = strings.TrimSpace(string(raw))
			}
			if briefValue == "" {
				return nil, usage("replication brief cannot be empty")
			}
			preserveValues := []any{}
			for _, item := range strings.Split(*preserve, ",") {
				if value := strings.TrimSpace(item); value != "" {
					preserveValues = append(preserveValues, value)
				}
			}
			referenceValues := make([]any, 0, len(references))
			for _, value := range references {
				referenceValues = append(referenceValues, map[string]any{"source": value, "role": "replacement identity or product reference"})
			}
			replace := map[string]any{}
			for key, value := range map[string]string{"subject": *subject, "product": *product, "setting": *setting, "style": *style} {
				if strings.TrimSpace(value) != "" {
					replace[key] = strings.TrimSpace(value)
				}
			}
			input = map[string]any{
				"version": "h3.replication/v1", "source": *source, "brief": briefValue,
				"title": *title, "preserve": preserveValues, "replace": replace,
				"references": referenceValues, "profile_id": *profile, "seed": *seed,
				"continuity": *continuity, "audio_policy": *audioPolicy,
			}
			if *steps > 0 {
				input["steps"] = *steps
			}
			if *loraStrength >= 0 {
				input["lora_strength"] = *loraStrength
			}
			if *promptFile != "" {
				raw, err := os.ReadFile(*promptFile)
				if err != nil {
					return nil, &contract.CLIError{Code: "local_file", Message: err.Error(), Cause: err}
				}
				if len(raw) == 0 {
					return nil, usage("--prompt-file cannot be empty")
				}
				input["prompt"] = string(raw)
			}
		}
		if *planOnly {
			return r.Service.PlanReplication(ctx, input)
		}
		input["to"] = *to
		input["force"] = *force
		input["detach"] = *detach
		return r.Service.ProduceReplication(ctx, input, operation.WaitOptions{
			Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event,
		})
	case "migrate-character":
		set := newFlags("video migrate-character")
		specPath := set.String("spec", "", "")
		source := set.String("source", "", "")
		character := set.String("character", "", "")
		subject := set.String("source-subject", "", "")
		details := set.String("details", "", "")
		detailsFile := set.String("details-file", "", "")
		promptFile := set.String("prompt-file", "", "")
		profile := set.String("profile", "minimax-h3-ref2va", "")
		steps := set.Int("steps", 0, "")
		loraStrength := set.Float64("lora-strength", -1, "")
		seed := set.Int("seed", -1, "")
		segmentFrames := set.Int("segment-frames", 243, "")
		overlapFrames := set.Int("overlap-frames", 39, "")
		audioPolicy := set.String("audio", "copy-source", "")
		to := set.String("to", "", "")
		detach := set.Bool("detach", false, "")
		planOnly := set.Bool("plan-only", false, "")
		force := set.Bool("force", false, "")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 5*time.Second, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 0 || *timeout < 0 || *poll < 0 {
			return nil, usage("video migrate-character accepts flags only; timeout and poll-interval cannot be negative")
		}
		if *detach && *planOnly {
			return nil, usage("--detach and --plan-only cannot be combined")
		}
		if !*planOnly && !*detach && *to == "" {
			return nil, usage("video migrate-character requires --to unless --plan-only or --detach is used")
		}
		visited := map[string]bool{}
		set.Visit(func(item *flag.Flag) { visited[item.Name] = true })
		if visited["steps"] && (*steps < 4 || *steps > 50) {
			return nil, usage("--steps must be between 4 and 50")
		}
		if visited["lora-strength"] && (*loraStrength < 0 || *loraStrength > 2) {
			return nil, usage("--lora-strength must be between 0 and 2")
		}
		if *seed < -1 {
			return nil, usage("--seed must be -1 or non-negative")
		}
		legalSegment := *segmentFrames >= 124 && *segmentFrames <= 362 && (*segmentFrames-5)%17 == 0
		if !legalSegment {
			return nil, usage("--segment-frames must be 17k+5 from 124 through 362")
		}
		legalOverlap := *overlapFrames == 5 || *overlapFrames == 22 || *overlapFrames == 39 || *overlapFrames == 56
		if !legalOverlap || *overlapFrames >= *segmentFrames {
			return nil, usage("--overlap-frames must be 5, 22, 39, or 56 and smaller than --segment-frames")
		}
		if *audioPolicy != "copy-source" && *audioPolicy != "reference-source" && *audioPolicy != "generate" && *audioPolicy != "mute" {
			return nil, usage("--audio must be copy-source, reference-source, generate, or mute")
		}
		var input map[string]any
		if *specPath != "" {
			for _, name := range []string{"source", "character", "source-subject", "details", "details-file", "prompt-file", "profile", "steps", "lora-strength", "seed", "segment-frames", "overlap-frames", "audio"} {
				if visited[name] {
					return nil, usage("--spec cannot be combined with --%s", name)
				}
			}
			var err error
			input, err = readJSONInput(r.Streams.In, *specPath)
			if err != nil {
				return nil, err
			}
		} else {
			if *source == "" || *character == "" || strings.TrimSpace(*subject) == "" {
				return nil, usage("video migrate-character requires --source, --character, and --source-subject")
			}
			if *details != "" && *detailsFile != "" {
				return nil, usage("--details and --details-file are mutually exclusive")
			}
			if *promptFile != "" && (*details != "" || *detailsFile != "") {
				return nil, usage("--prompt-file is a complete expert prompt and cannot be combined with details")
			}
			detailValue := *details
			if *detailsFile != "" {
				raw, err := os.ReadFile(*detailsFile)
				if err != nil {
					return nil, &contract.CLIError{Code: "local_file", Message: err.Error(), Cause: err}
				}
				detailValue = strings.TrimSpace(string(raw))
			}
			input = map[string]any{
				"version": "h3.character-migration/v1", "source": *source,
				"targets":    []any{map[string]any{"character": *character, "source_subject": strings.TrimSpace(*subject)}},
				"profile_id": *profile, "seed": *seed, "segment_frames": *segmentFrames,
				"overlap_frames": *overlapFrames, "audio_policy": *audioPolicy,
			}
			if detailValue != "" {
				input["targets"].([]any)[0].(map[string]any)["details"] = detailValue
			}
			if *promptFile != "" {
				raw, err := os.ReadFile(*promptFile)
				if err != nil {
					return nil, &contract.CLIError{Code: "local_file", Message: err.Error(), Cause: err}
				}
				if len(raw) == 0 {
					return nil, usage("--prompt-file cannot be empty")
				}
				input["prompt"] = string(raw)
			}
			if *steps > 0 {
				input["steps"] = *steps
			}
			if *loraStrength >= 0 {
				input["lora_strength"] = *loraStrength
			}
		}
		if *planOnly {
			return r.Service.PlanCharacterMigration(ctx, input)
		}
		input["to"] = *to
		input["force"] = *force
		input["detach"] = *detach
		return r.Service.ProduceCharacterMigration(ctx, input, operation.WaitOptions{
			Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event,
		})
	case "trim":
		return r.runMedia(ctx, append([]string{"trim"}, args[1:]...))
	case "concat":
		return r.runProject(ctx, append([]string{"merge"}, args[1:]...))
	default:
		return nil, usage("unknown video command %q", args[0])
	}
}
