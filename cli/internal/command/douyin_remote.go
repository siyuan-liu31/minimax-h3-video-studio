package command

import (
	"context"
	"fmt"
	"h3studio/cli/internal/douyin"
	"h3studio/cli/internal/operation"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func (r *Runner) runDouyinRemote(ctx context.Context, args []string) (any, error) {
	action := args[0]
	switch action {
	case "capabilities", "list":
		if len(args) != 1 {
			return nil, usage("douyin %s accepts no arguments", action)
		}
		endpoint := "tasks"
		if action == "capabilities" {
			endpoint = "capabilities"
		}
		return r.Service.API.Get(ctx, "/api/douyin/"+endpoint)
	case "status", "cancel", "retry":
		if len(args) != 2 {
			return nil, usage("douyin %s requires TASK", action)
		}
		id, err := voiceTaskID(args[1])
		if err != nil {
			return nil, err
		}
		name := "douyin.get"
		if action != "status" {
			name = "douyin." + action
		}
		return operation.Execute(ctx, operation.Runtime{Service: r.Service}, name, map[string]any{"task_id": id})
	case "wait":
		set := newFlags("douyin wait")
		timeout := set.Duration("timeout", 0, "")
		poll := set.Duration("poll-interval", 2*time.Second, "")
		if err := parseFlags(set, args[1:]); err != nil || set.NArg() != 1 || *timeout < 0 || *poll <= 0 {
			return nil, usage("douyin wait requires TASK and valid durations")
		}
		return r.Service.WaitDouyin(ctx, set.Arg(0), operation.WaitOptions{Timeout: *timeout, PollInterval: *poll, OnEvent: r.Printer.Event})
	case "import", "inspect":
		set := newFlags("douyin " + action)
		local := set.Bool("local", false, "download on this computer, then upload")
		browser := set.String("cookies-from-browser", "", "")
		executable := set.String("yt-dlp", "", "")
		detach := set.Bool("detach", false, "")
		quality := set.String("quality", "best", "")
		timeout := set.Duration("timeout", 10*time.Minute, "")
		if err := parseFlags(set, args[1:]); err != nil || set.NArg() == 0 || *timeout <= 0 {
			return nil, usage("douyin %s requires TEXT and a positive timeout", action)
		}
		text := strings.Join(set.Args(), " ")
		if _, err := douyin.ExtractURL(text); err != nil {
			return nil, douyinCLIError(err)
		}
		if *quality != "best" && *quality != "1080" && *quality != "720" {
			return nil, usage("quality must be best, 1080 or 720")
		}
		if *local {
			if action != "import" || *detach || *quality != "best" {
				return nil, usage("--local requires import without --detach or --quality")
			}
			client, err := douyin.New(douyin.Config{Executable: *executable, CookiesFromBrowser: *browser, Timeout: *timeout})
			if err != nil {
				return nil, douyinCLIError(err)
			}
			dir, err := os.MkdirTemp("", "h3-douyin-import-")
			if err != nil {
				return nil, err
			}
			defer os.RemoveAll(dir)
			result, err := client.Download(ctx, text, filepath.Join(dir, "video.%(ext)s"), false)
			if err != nil {
				return nil, douyinCLIError(err)
			}
			uploaded, err := r.Service.Upload(ctx, result.Path, "video")
			if err != nil {
				return nil, err
			}
			return map[string]any{"asset": uploaded, "source": "local_douyin"}, nil
		}
		if *browser != "" || *executable != "" {
			return nil, usage("browser cookies and executable options require --local")
		}
		mode := "download"
		if action == "inspect" {
			mode = "parse"
		}
		input := map[string]any{"text": text, "mode": mode, "quality": *quality}
		if r.Globals.RequestID != "" {
			input["request_id"] = r.Globals.RequestID
		}
		task, err := r.Service.SubmitDouyin(ctx, input)
		if err != nil {
			return nil, err
		}
		id := stringAny(task["task_id"], "")
		r.Printer.Event(map[string]any{"type": "submitted", "task_id": id})
		if *detach {
			return task, nil
		}
		return r.Service.WaitDouyin(ctx, id, operation.WaitOptions{Timeout: *timeout, OnEvent: r.Printer.Event})
	}
	return nil, fmt.Errorf("unknown Douyin action")
}
