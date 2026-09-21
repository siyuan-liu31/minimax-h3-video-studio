package command

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/operation"
	"h3studio/cli/internal/transfer"
)

const GenerateHelp = `Usage: h3ctl generate image|video [flags]

Image flags:
  --prompt TEXT | --prompt-file PATH|-     Prompt input
  --ref LOCATOR                           Repeatable image reference
  --profile ID                            auto or profile ID
  --aspect-ratio RATIO | --width N --height N
  --steps N --seed N --cfg N --denoise N --negative-prompt TEXT
  Qwen-Image 2.1 BF16: --profile qwen-image-2.1-bf16;
    no --ref = text-to-image, ordered --ref = instruction-based image editing.
    Use <image1>, <image2> (or 图1、图2) in the prompt; up to 10 images.

Video flags:
  --mode t2v|i2v|fl2v|r2v|v2v|rv2v      Required explicit Director mode
  --prompt-mode default|preserve_tags_only  Default: preserve_tags_only
  --first-frame LOCATOR --last-frame LOCATOR
  --source-video LOCATOR
  --ref LOCATOR | 'json:{"role":"ROLE","source":"LOCATOR"}'
                                              Repeatable; H3 allows 12 files total
  --ref-dir 'json:{"role":"ROLE","path":"DIR"}'  Stable directory references
  --duration SECONDS --aspect-ratio RATIO --steps N --seed N

Shared:
  --ref-dir 'json:{"role":"ROLE","path":"DIR"}'  Expand a directory; legacy CSV is supported
  --spec PATH|-        Versioned JSON request; typed generation flags may not mix
  --wait               Wait after submission (Ctrl-C does not cancel remote job)
  --wait-timeout 2h    Total wait; 0 means unlimited
  --poll-interval 5s
  --download PATH      Implies --wait
  --force              Allow replacing download destination

Locators: local files, file://, asset:ID, job:ID#INDEX, media:ID,
or h3://CONTEXT/assets/ID. Local inputs are uploaded before submission.
Defaults: profile=auto, seed=-1, poll-interval=5s, wait-timeout=0.
Example: h3ctl generate video --mode t2v --prompt 'A sunrise' --wait
Example: h3ctl generate image --profile qwen-image-2.1-bf16 --prompt 'A ceramic teapot' --wait
`

type generateFlags struct {
	spec, prompt, promptFile, profile, aspect, negative, mode, promptMode, first, last, source, download string
	width, height, steps                                                                                 int
	seed                                                                                                 int64
	duration, cfg, denoise                                                                               float64
	wait, force                                                                                          bool
	waitTimeout, poll                                                                                    time.Duration
	refs, refDirs                                                                                        stringsFlag
}

func (r *Runner) runGenerate(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, GenerateHelp)
		return nil, nil
	}
	kind := args[0]
	if kind != "image" && kind != "video" {
		return nil, usage("generate requires image or video")
	}
	set := newFlags("generate " + kind)
	f := generateFlags{profile: "auto", seed: -1}
	set.StringVar(&f.spec, "spec", "", "")
	set.StringVar(&f.prompt, "prompt", "", "")
	set.StringVar(&f.promptFile, "prompt-file", "", "")
	set.StringVar(&f.profile, "profile", "auto", "")
	set.StringVar(&f.aspect, "aspect-ratio", "", "")
	set.StringVar(&f.negative, "negative-prompt", "", "")
	set.StringVar(&f.mode, "mode", "", "")
	set.StringVar(&f.promptMode, "prompt-mode", "", "")
	set.StringVar(&f.first, "first-frame", "", "")
	set.StringVar(&f.last, "last-frame", "", "")
	set.StringVar(&f.source, "source-video", "", "")
	set.StringVar(&f.download, "download", "", "")
	set.IntVar(&f.width, "width", 0, "")
	set.IntVar(&f.height, "height", 0, "")
	set.IntVar(&f.steps, "steps", 0, "")
	set.Int64Var(&f.seed, "seed", -1, "")
	set.Float64Var(&f.duration, "duration", 0, "")
	set.Float64Var(&f.cfg, "cfg", 0, "")
	set.Float64Var(&f.denoise, "denoise", 0, "")
	set.BoolVar(&f.wait, "wait", false, "")
	set.BoolVar(&f.force, "force", false, "")
	set.DurationVar(&f.waitTimeout, "wait-timeout", 0, "")
	set.DurationVar(&f.poll, "poll-interval", 5*time.Second, "")
	set.Var(&f.refs, "ref", "")
	set.Var(&f.refDirs, "ref-dir", "")
	if err := parseFlags(set, args[1:], "ref", "ref-dir"); err != nil {
		return nil, usage("%v", err)
	}
	if set.NArg() != 0 {
		return nil, usage("unexpected arguments: %s", strings.Join(set.Args(), " "))
	}
	visited := map[string]bool{}
	set.Visit(func(item *flag.Flag) { visited[item.Name] = true })
	if f.waitTimeout < 0 {
		return nil, usage("--wait-timeout cannot be negative")
	}
	if f.poll <= 0 {
		return nil, usage("--poll-interval must be positive")
	}
	if visited["seed"] && f.seed < -1 {
		return nil, usage("--seed must be -1 or non-negative")
	}
	if visited["cfg"] && (f.cfg <= 0 || math.IsNaN(f.cfg) || math.IsInf(f.cfg, 0)) {
		return nil, usage("--cfg must be a positive finite number")
	}
	if visited["denoise"] && (f.denoise <= 0 || math.IsNaN(f.denoise) || math.IsInf(f.denoise, 0)) {
		return nil, usage("--denoise must be a positive finite number")
	}
	if kind == "image" {
		for _, name := range []string{"mode", "prompt-mode", "first-frame", "last-frame", "source-video", "duration"} {
			if visited[name] {
				return nil, usage("--%s is not valid for image generation", name)
			}
		}
	} else {
		for _, name := range []string{"negative-prompt", "cfg"} {
			if visited[name] {
				return nil, usage("--%s is not valid for video generation", name)
			}
		}
	}
	payload := map[string]any{}
	resolved := []any{}
	if f.spec != "" {
		raw, err := readInput(r.Streams.In, f.spec)
		if err != nil {
			return nil, err
		}
		if err := json.Unmarshal(raw, &payload); err != nil {
			return nil, &contract.CLIError{Code: "invalid_spec", Message: err.Error(), Cause: err}
		}
		clientOnly := map[string]bool{"spec": true, "wait": true, "wait-timeout": true, "poll-interval": true, "download": true, "force": true}
		for name := range visited {
			if !clientOnly[name] {
				return nil, usage("--spec cannot be combined with typed generation flag --%s", name)
			}
		}
		if declared := stringAny(payload["output_type"], kind); declared != kind {
			return nil, usage("--spec output_type %q conflicts with generate %s", declared, kind)
		}
		payload["output_type"] = kind
	}
	if f.spec == "" {
		prompt, err := resolvePrompt(r.Streams.In, f.prompt, f.promptFile)
		if err != nil {
			return nil, err
		}
		if strings.TrimSpace(prompt) == "" {
			return nil, usage("--prompt or --prompt-file is required")
		}
		payload["output_type"] = kind
		payload["prompt"] = prompt
		payload["profile_id"] = f.profile
		parameters := map[string]any{}
		if f.aspect != "" {
			parameters["aspect_ratio"] = f.aspect
		}
		if f.width != 0 || f.height != 0 {
			if f.width <= 0 || f.height <= 0 {
				return nil, usage("--width and --height must be positive and supplied together")
			}
			parameters["width"] = f.width
			parameters["height"] = f.height
		}
		if f.steps > 0 {
			parameters["steps"] = f.steps
		}
		if visited["steps"] && f.steps <= 0 {
			return nil, usage("--steps must be positive")
		}
		if f.seed >= 0 {
			parameters["seed"] = f.seed
		}
		if f.cfg > 0 {
			parameters["cfg"] = f.cfg
		}
		if f.denoise > 0 {
			parameters["denoise"] = f.denoise
		}
		if f.duration > 0 {
			parameters["duration"] = f.duration
		}
		if visited["duration"] && (f.duration <= 0 || math.IsNaN(f.duration) || math.IsInf(f.duration, 0)) {
			return nil, usage("--duration must be positive")
		}
		if f.negative != "" {
			payload["negative_prompt"] = f.negative
		}
		payload["parameters"] = parameters
		referenceInputs := []referenceInput{}
		for _, raw := range f.refs {
			parsed, err := parseReference(raw)
			if err != nil {
				return nil, usage("invalid --ref %q: %v", raw, err)
			}
			referenceInputs = append(referenceInputs, parsed)
		}
		for _, raw := range f.refDirs {
			role, path, err := parseRefDir(raw)
			if err != nil {
				return nil, usage("invalid --ref-dir %q: %v", raw, err)
			}
			files, err := transfer.Collect(path, true, nil)
			if err != nil {
				return nil, err
			}
			for _, file := range files {
				referenceInputs = append(referenceInputs, referenceInput{Role: role, Source: file})
			}
		}
		if kind == "video" {
			if !validVideoMode(f.mode) {
				return nil, usage("--mode must be t2v, i2v, fl2v, r2v, v2v, or rv2v")
			}
			payload["director_mode"] = f.mode
			payload["prompt_mode"] = "preserve_tags_only"
			if f.promptMode != "" {
				if f.promptMode != "default" && f.promptMode != "preserve_tags_only" {
					return nil, usage("--prompt-mode must be default or preserve_tags_only")
				}
				payload["prompt_mode"] = f.promptMode
			}
			if f.first != "" {
				referenceInputs = append(referenceInputs, referenceInput{Role: "first_frame", Source: f.first})
			}
			if f.last != "" {
				referenceInputs = append(referenceInputs, referenceInput{Role: "last_frame", Source: f.last})
			}
			if f.source != "" {
				referenceInputs = append([]referenceInput{{Role: "motion", Source: f.source, SourceVideo: true}}, referenceInputs...)
			}
		}
		maximumReferences := 10
		if kind == "video" {
			maximumReferences = 12
		}
		if len(referenceInputs) > maximumReferences {
			return nil, usage("at most %d total references are allowed", maximumReferences)
		}
		if kind == "video" {
			if err := validateDirector(f.mode, referenceInputs); err != nil {
				return nil, err
			}
		}
		references := make([]any, 0, len(referenceInputs))
		for index, input := range referenceInputs {
			references = append(references, map[string]any{"source": input.Source, "role": input.Role, "reference_index": index})
			if input.SourceVideo {
				payload["source_asset_id"] = input.Source
			}
		}
		payload["references"] = references
	}
	if r.Globals.RequestID != "" {
		payload["request_id"] = r.Globals.RequestID
	} else if stringAny(payload["request_id"], "") == "" {
		payload["request_id"] = newRequestID()
	}
	submitted, err := r.Service.Generate(ctx, payload)
	if err != nil {
		return nil, err
	}
	jobID := stringAny(submitted["job_id"], stringAny(submitted["id"], ""))
	if values, ok := submitted["resolved_resources"].([]any); ok {
		resolved = values
	}
	delete(submitted, "resolved_resources")
	requestID := stringAny(payload["request_id"], "")
	result := map[string]any{"submitted": submitted, "job_id": jobID, "request_id": requestID, "resolved_resources": resolved}
	if !f.wait && f.download == "" {
		return result, nil
	}
	if jobID == "" {
		return nil, contract.NewError("invalid_response", "generation response did not contain job_id")
	}
	r.Printer.Event(map[string]any{"type": "submitted", "job_id": jobID, "request_id": requestID, "submission": submitted})
	completed, err := r.Service.Wait(ctx, jobID, operation.WaitOptions{Timeout: f.waitTimeout, PollInterval: f.poll, OnEvent: r.Printer.Event})
	if err != nil {
		return nil, withSubmission(err, jobID, requestID, submitted)
	}
	result["completed"] = completed
	if f.download != "" {
		downloaded, err := r.Service.API.Download(ctx, "/api/download?id="+url.QueryEscape(jobID)+"&index=0", f.download, f.force)
		if err != nil {
			return nil, withSubmission(err, jobID, requestID, submitted)
		}
		result["download"] = downloaded
	}
	return result, nil
}

func withSubmission(err error, jobID, requestID string, submission map[string]any) error {
	var typed *contract.CLIError
	if errors.As(err, &typed) {
		details := map[string]any{"job_id": jobID, "request_id": requestID, "submission": submission}
		if existing, ok := typed.Details.(map[string]any); ok {
			for key, value := range existing {
				details[key] = value
			}
		}
		clone := *typed
		clone.Details = details
		return &clone
	}
	return &contract.CLIError{Code: "operation_failed", Message: err.Error(), Details: map[string]any{"job_id": jobID, "request_id": requestID, "submission": submission}, Cause: err}
}

type referenceInput struct {
	Role, Source string
	SourceVideo  bool
}

func parseReference(raw string) (referenceInput, error) {
	trimmed := strings.TrimSpace(raw)
	if !strings.HasPrefix(trimmed, "json:") {
		return referenceInput{Role: "reference", Source: raw}, nil
	}
	trimmed = strings.TrimPrefix(trimmed, "json:")
	var object map[string]any
	if err := json.Unmarshal([]byte(trimmed), &object); err != nil {
		return referenceInput{}, fmt.Errorf("structured --ref json: value must be an object: %w", err)
	}
	if len(object) > 2 {
		return referenceInput{}, fmt.Errorf("structured --ref accepts only role and source")
	}
	item := referenceInput{Role: stringAny(object["role"], "reference"), Source: stringAny(object["source"], "")}
	for key := range object {
		if key != "role" && key != "source" {
			return item, fmt.Errorf("structured --ref has unknown field %q", key)
		}
	}
	if item.Source == "" {
		return item, fmt.Errorf("source is required")
	}
	return item, nil
}
func parseRefDir(raw string) (string, string, error) {
	trimmed := strings.TrimSpace(raw)
	if strings.HasPrefix(trimmed, "json:") {
		var object map[string]any
		if err := json.Unmarshal([]byte(strings.TrimPrefix(trimmed, "json:")), &object); err != nil {
			return "", "", fmt.Errorf("structured --ref-dir json: value must be an object: %w", err)
		}
		for key := range object {
			if key != "role" && key != "path" {
				return "", "", fmt.Errorf("structured --ref-dir has unknown field %q", key)
			}
		}
		role, directory := stringAny(object["role"], "reference"), stringAny(object["path"], "")
		if directory == "" {
			return "", "", fmt.Errorf("path is required")
		}
		return role, directory, nil
	}
	role, path := "reference", ""
	for _, part := range strings.Split(raw, ",") {
		key, value, ok := strings.Cut(part, "=")
		if !ok {
			return "", "", fmt.Errorf("expected role=ROLE,path=DIR")
		}
		switch key {
		case "role":
			role = value
		case "path":
			path = value
		default:
			return "", "", fmt.Errorf("unknown key %q", key)
		}
	}
	if path == "" {
		return "", "", fmt.Errorf("path is required")
	}
	return role, path, nil
}
func validVideoMode(mode string) bool {
	return mode == "t2v" || mode == "i2v" || mode == "fl2v" || mode == "r2v" || mode == "v2v" || mode == "rv2v"
}
func validateDirector(mode string, refs []referenceInput) error {
	endpoints, videos, others := 0, 0, 0
	for _, ref := range refs {
		if ref.SourceVideo {
			videos++
		} else if ref.Role == "first_frame" || ref.Role == "last_frame" {
			endpoints++
		} else {
			others++
		}
	}
	switch mode {
	case "t2v":
		if len(refs) != 0 {
			return usage("t2v does not accept references")
		}
	case "i2v":
		if endpoints != 1 || len(refs) != 1 || refs[0].Role != "first_frame" {
			return usage("i2v requires exactly --first-frame")
		}
	case "fl2v":
		if endpoints < 1 || endpoints > 2 || others+videos > 0 {
			return usage("fl2v requires one or two endpoint frames only")
		}
	case "r2v":
		if len(refs) == 0 || videos > 0 {
			return usage("r2v requires --ref inputs and no --source-video")
		}
	case "v2v":
		if videos != 1 || len(refs) != 1 {
			return usage("v2v requires exactly --source-video")
		}
	case "rv2v":
		if videos != 1 || len(refs) < 2 {
			return usage("rv2v requires --source-video plus image/audio references")
		}
	}
	return nil
}
func resolvePrompt(in io.Reader, prompt, path string) (string, error) {
	if prompt != "" && path != "" {
		return "", usage("use only one of --prompt or --prompt-file")
	}
	if path == "" {
		return prompt, nil
	}
	raw, err := readInput(in, path)
	return string(raw), err
}
func readInput(in io.Reader, path string) ([]byte, error) {
	if path == "-" {
		return io.ReadAll(in)
	}
	return os.ReadFile(path)
}
func newRequestID() string {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(raw)
}
