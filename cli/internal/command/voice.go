package command

import (
	"context"
	"flag"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
	"unicode/utf8"

	"h3studio/cli/internal/operation"
	"h3studio/cli/internal/resource"
)

const VoiceHelp = `Usage: h3ctl voice COMMAND

  convert SOURCE --reference AUDIO --engine vevo2|yingmusic [--steps 100] [--cfg 0.7] [--seed -1] [--keep-stems] [--echo=false] [--reverb=false] [--detach] [--to PATH]
  rewrite SOURCE --lyrics-file FILE [--original-lyrics-file FILE] [--reference AUDIO] [--preview] [--steps 32] [--cfg 3] [--seed -1] [--detach] [--to PATH]
  status TASK
  wait TASK [--timeout DURATION] [--poll-interval DURATION]
  cancel TASK
  delete TASK
  download TASK --to PATH [--track mix|dry_vocal|accompaniment|remix] [--force]
  capabilities

vevo2 uses the reviewed FM-only style-preserved VC/SVC path.
yingmusic runs the official separation, singing conversion, and remix pipeline.
For yingmusic, --seed -1 chooses a new seed on each task; the receipt records the effective seed.
--keep-stems retains dry converted vocals and accompaniment for separate download.
--to on convert downloads the final mix; use voice download --track for retained stems.
SOURCE and --reference accept local files, asset:ID, job:ID#INDEX, or media:ID.
Conversion waits by default; --detach returns after durable queue submission.
Example: h3ctl voice convert song.wav --reference singer.wav --engine yingmusic --to converted.wav
`

func (r *Runner) runVoice(ctx context.Context, args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, VoiceHelp)
		return nil, nil
	}
	switch args[0] {
	case "rewrite":
		return r.runVoiceRewrite(ctx, args[1:])
	case "convert":
		set := newFlags("voice convert")
		engine := set.String("engine", "", "")
		reference := set.String("reference", "", "")
		detach := set.Bool("detach", false, "")
		to := set.String("to", "", "")
		force := set.Bool("force", false, "")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 5*time.Second, "")
		steps := set.Int("steps", 100, "")
		cfg := set.Float64("cfg", 0.7, "")
		seed := set.Int64("seed", -1, "")
		keepStems := set.Bool("keep-stems", false, "")
		echo := set.Bool("echo", true, "")
		reverb := set.Bool("reverb", true, "")
		if err := parseFlags(set, args[1:]); err != nil {
			return nil, usage("%v", err)
		}
		if set.NArg() != 1 || *reference == "" || (*engine != "vevo2" && *engine != "yingmusic") {
			return nil, usage("voice convert requires SOURCE, --reference AUDIO, and --engine vevo2|yingmusic")
		}
		if *timeout < 0 || *poll <= 0 || (*detach && *to != "") {
			return nil, usage("timeouts must be valid and --detach cannot be combined with --to")
		}
		tuning := map[string]any{}
		outputOptionsSpecified := false
		set.Visit(func(flag *flag.Flag) {
			switch flag.Name {
			case "steps":
				tuning["diffusion_steps"] = *steps
			case "cfg":
				tuning["inference_cfg_rate"] = *cfg
			case "seed":
				tuning["seed"] = *seed
			case "keep-stems", "echo", "reverb":
				outputOptionsSpecified = true
			}
		})
		if *engine != "yingmusic" && (len(tuning) > 0 || outputOptionsSpecified) {
			return nil, usage("--steps, --cfg, --seed, --keep-stems, --echo and --reverb are only supported for yingmusic")
		}
		if *engine == "yingmusic" && (*steps < 10 || *steps > 200 || math.IsNaN(*cfg) || math.IsInf(*cfg, 0) || *cfg < 0 || *cfg > 2 || *seed < -1 || *seed > 4294967295) {
			return nil, usage("yingmusic tuning requires --steps 10..200, --cfg 0..2 and --seed -1 or 0..4294967295")
		}
		if outputOptionsSpecified {
			tuning["output_options"] = map[string]any{"include_stems": *keepStems, "echo": *echo, "reverb": *reverb}
		}
		submitted, err := r.Service.SubmitVoice(ctx, *engine, set.Arg(0), *reference, r.Globals.RequestID, tuning)
		if err != nil {
			return nil, err
		}
		taskID := stringAny(submitted["task_id"], "")
		result := map[string]any{"submitted": submitted, "task_id": taskID}
		if *detach {
			return result, nil
		}
		completed, err := r.Service.WaitVoice(ctx, taskID, operation.WaitOptions{Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event})
		if err != nil {
			return nil, err
		}
		result["completed"] = completed
		if *to != "" {
			downloaded, err := r.Service.API.Download(ctx, "/api/voice/tasks/"+url.PathEscape(taskID)+"/download", *to, *force)
			if err != nil {
				return nil, err
			}
			result["download"] = downloaded
		}
		return result, nil
	case "status":
		if len(args) != 2 {
			return nil, usage("voice status requires TASK")
		}
		id, err := voiceTaskID(args[1])
		if err != nil {
			return nil, err
		}
		return r.Service.API.Get(ctx, "/api/voice/tasks/"+url.PathEscape(id))
	case "wait":
		set := newFlags("voice wait")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 5*time.Second, "")
		if err := parseFlags(set, args[1:]); err != nil || set.NArg() != 1 || *timeout < 0 || *poll <= 0 {
			return nil, usage("voice wait requires TASK and valid durations")
		}
		id, err := voiceTaskID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		return r.Service.WaitVoice(ctx, id, operation.WaitOptions{Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event})
	case "cancel":
		if len(args) != 2 {
			return nil, usage("voice cancel requires TASK")
		}
		id, err := voiceTaskID(args[1])
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodPost, "/api/voice/tasks/"+url.PathEscape(id)+"/cancel", nil, &value)
		return value, err
	case "delete":
		if len(args) != 2 {
			return nil, usage("voice delete requires TASK")
		}
		id, err := voiceTaskID(args[1])
		if err != nil {
			return nil, err
		}
		value := map[string]any{}
		err = r.Service.API.JSON(ctx, http.MethodDelete, "/api/voice/tasks/"+url.PathEscape(id), nil, &value)
		return value, err
	case "download":
		set := newFlags("voice download")
		to := set.String("to", "", "")
		track := set.String("track", "mix", "")
		force := set.Bool("force", false, "")
		if err := parseFlags(set, args[1:]); err != nil || set.NArg() != 1 || *to == "" || !validVoiceTrack(*track) {
			return nil, usage("voice download requires TASK --to PATH and --track mix|dry_vocal|accompaniment|remix")
		}
		id, err := voiceTaskID(set.Arg(0))
		if err != nil {
			return nil, err
		}
		return r.Service.API.Download(ctx, voiceDownloadPath(id, *track), *to, *force)
	case "capabilities":
		if len(args) != 1 {
			return nil, usage("voice capabilities does not accept arguments")
		}
		return r.Service.API.Get(ctx, "/api/voice/capabilities")
	default:
		return nil, usage("unknown voice command %q", args[0])
	}
}

func voiceTaskID(raw string) (string, error) {
	if !resource.ValidServerID(raw) {
		return "", usage("voice task id must be 32 lowercase hex characters")
	}
	return raw, nil
}

func validVoiceTrack(track string) bool {
	return track == "mix" || track == "dry_vocal" || track == "accompaniment" || track == "remix"
}

func voiceDownloadPath(taskID, track string) string {
	path := "/api/voice/tasks/" + url.PathEscape(taskID) + "/download"
	if track != "mix" {
		path += "?track=" + url.QueryEscape(track)
	}
	return path
}

// runVoiceRewrite keeps the CLI file handling separate from the reusable API operation.
func (r *Runner) runVoiceRewrite(ctx context.Context, args []string) (any, error) {
	set := newFlags("voice rewrite")
	preview := set.Bool("preview", false, "generate first segment only")
	lyricsFile := set.String("lyrics-file", "", "UTF-8 lyrics file")
	originalFile := set.String("original-lyrics-file", "", "optional original lyrics")
	reference := set.String("reference", "", "defaults to source")
	steps := set.Int("steps", 32, "")
	cfg := set.Float64("cfg", 3, "")
	seed := set.Int64("seed", -1, "")
	detach := set.Bool("detach", false, "")
	to := set.String("to", "", "")
	force := set.Bool("force", false, "")
	timeout := set.Duration("timeout", 0, "")
	poll := set.Duration("poll-interval", 5*time.Second, "")
	if err := parseFlags(set, args); err != nil {
		return nil, usage("%v", err)
	}
	if set.NArg() != 1 || *lyricsFile == "" || *timeout < 0 || *poll <= 0 || (*detach && *to != "") {
		return nil, usage("voice rewrite requires SOURCE --lyrics-file FILE; --detach cannot be combined with --to")
	}
	readLyrics := func(path string) (string, error) {
		data, err := os.ReadFile(path)
		if err != nil {
			return "", err
		}
		text := strings.TrimSpace(string(data))
		if !utf8.Valid(data) || text == "" || utf8.RuneCountInString(text) > 10000 {
			return "", usage("lyrics must be nonempty UTF-8 text, at most 10000 characters")
		}
		return text, nil
	}
	lyrics, err := readLyrics(*lyricsFile)
	if err != nil {
		return nil, err
	}
	original := ""
	if *originalFile != "" {
		original, err = readLyrics(*originalFile)
		if err != nil {
			return nil, err
		}
	}
	ref := *reference
	if ref == "" {
		ref = set.Arg(0)
	}
	input := map[string]any{"preview": *preview, "source": set.Arg(0), "reference": ref, "lyrics": lyrics, "original_lyrics": original, "diffusion_steps": float64(*steps), "inference_cfg_rate": *cfg, "seed": float64(*seed)}
	submitted, err := r.Service.SubmitRewrite(ctx, input, r.Globals.RequestID)
	if err != nil {
		return nil, err
	}
	taskID := stringAny(submitted["task_id"], "")
	result := map[string]any{"task_id": taskID, "submitted": submitted}
	if *detach {
		return result, nil
	}
	completed, err := r.Service.WaitVoice(ctx, taskID, operation.WaitOptions{Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event})
	if err != nil {
		return nil, err
	}
	result["completed"] = completed
	if *to != "" {
		downloaded, err := r.Service.API.Download(ctx, voiceDownloadPath(taskID, "mix"), *to, *force)
		if err != nil {
			return nil, err
		}
		result["download"] = downloaded
	}
	return result, nil
}
