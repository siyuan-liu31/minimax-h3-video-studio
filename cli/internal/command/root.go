package command

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"h3studio/cli/internal/api"
	"h3studio/cli/internal/config"
	"h3studio/cli/internal/connection"
	"h3studio/cli/internal/contract"
	"h3studio/cli/internal/operation"
	"h3studio/cli/internal/output"
)

const Version = "0.6.1-dev.20260925.4"

type IOStreams struct {
	In       io.Reader
	Out, Err io.Writer
}
type Globals struct {
	Context, Server, Format, RequestID            string
	ControlTimeout, TransferTimeout, MediaTimeout time.Duration
	NonInteractive, NoColor, Quiet                bool
}
type Runner struct {
	Streams           IOStreams
	Store             config.Store
	Globals           Globals
	Printer           output.Printer
	Service           *operation.Service
	Session           *connection.Session
	ConnectionOptions connection.Options
}

func Execute(ctx context.Context, args []string, streams IOStreams) int {
	return executeWithConnectionOptions(ctx, args, streams, connection.Options{})
}

func executeWithConnectionOptions(ctx context.Context, args []string, streams IOStreams, connectionOptions connection.Options) int {
	if streams.In == nil {
		streams.In = os.Stdin
	}
	if streams.Out == nil {
		streams.Out = os.Stdout
	}
	if streams.Err == nil {
		streams.Err = os.Stderr
	}
	path, err := config.DefaultPath()
	if err != nil {
		fmt.Fprintln(streams.Err, err)
		return 1
	}
	initialFormat := preparseFormat(args)
	runner := &Runner{Streams: streams, Store: config.Store{Path: path}, ConnectionOptions: connectionOptions}
	runner.Printer = output.Printer{Stdout: streams.Out, Stderr: streams.Err, Format: initialFormat}
	command, rest, err := runner.parseGlobals(args)
	if err != nil {
		runner.Printer.Failure("h3ctl", err)
		return contract.ExitCode(err)
	}
	if command == "" || command == "help" || command == "--help" || command == "-h" {
		fmt.Fprint(streams.Out, RootHelp)
		return 0
	}
	connected := false
	if runner.commandNeedsConnection(command, rest) {
		if err := runner.connect(ctx); err != nil {
			runner.Printer.Failure(command, err)
			return contract.ExitCode(err)
		}
		connected = true
	}
	result, dispatchErr := runner.dispatch(ctx, command, rest)
	var cleanupErr error
	if connected {
		cleanupErr = runner.closeConnection()
	}
	err = mergeCleanupError(dispatchErr, cleanupErr)
	if err != nil {
		runner.Printer.Failure(commandPath(command, rest), err)
		return contract.ExitCode(err)
	}
	if result != nil {
		if err := runner.Printer.Result(commandPath(command, rest), result); err != nil {
			fmt.Fprintln(streams.Err, err)
			return 1
		}
	}
	return 0
}

func (r *Runner) dispatch(ctx context.Context, command string, rest []string) (any, error) {
	switch command {
	case "version":
		if explicitHelp(rest) {
			fmt.Fprint(r.Streams.Out, "Usage: h3ctl version\nPrints the CLI version. Defaults: no command flags.\nExample: h3ctl version --json\n")
			return nil, nil
		}
		if len(rest) > 0 {
			return nil, usage("version does not accept arguments")
		}
		return map[string]any{"version": Version}, nil
	case "context":
		return r.runContext(ctx, rest)
	case "doctor":
		return r.runDoctor(ctx, rest)
	case "capability":
		return r.runCapability(ctx, rest)
	case "profile":
		return r.runProfile(ctx, rest)
	case "asset":
		return r.runAsset(ctx, rest)
	case "generate":
		return r.runGenerate(ctx, rest)
	case "job":
		return r.runJob(ctx, rest)
	case "media":
		return r.runMedia(ctx, rest)
	case "video":
		return r.runVideo(ctx, rest)
	case "replication":
		return r.runReplication(ctx, rest)
	case "voice":
		return r.runVoice(ctx, rest)
	case "project":
		return r.runProject(ctx, rest)
	case "operation":
		return r.runOperation(ctx, rest)
	case "douyin":
		return r.runDouyin(ctx, rest)
	case "workflow":
		return r.runWorkflow(rest)
	case "completion":
		if help(rest) {
			fmt.Fprint(r.Streams.Out, "Usage: h3ctl completion [bash|zsh|fish]\n\nShell completion is reserved; this help is stable. Defaults: no flags.\nExample: h3ctl completion --help\n")
			return nil, nil
		}
		return nil, contract.Unsupported("completion", "shell completion generation is reserved for a later CLI version")
	default:
		return nil, usage("unknown command %q", command)
	}
}

var networkCommandActions = map[string]map[string]bool{
	"replication": {"plan": true, "create": true, "list": true, "inspect": true, "export": true, "edit-segment": true, "run": true, "wait": true, "stop": true, "rerun": true, "merge": true, "download": true, "resume": true},
	"capability":  {"list": true, "show": true},
	"profile":     {"list": true, "show": true},
	"asset":       {"upload": true, "download": true, "list": true, "get": true, "copy": true, "update": true, "pin": true, "delete": true},
	"generate":    {"image": true, "video": true},
	"job":         {"list": true, "get": true, "wait": true, "resume": true, "cancel": true, "download": true, "save": true, "workflow": true, "delete": true},
	"media":       {"frame": true, "endpoints": true, "trim": true, "extract-audio": true, "remove-audio": true, "mux-audio": true, "prepare-reference": true, "list": true, "get": true, "download": true, "save": true, "delete": true},
	"video":       {"compose": true, "replicate": true, "migrate-character": true, "trim": true, "concat": true},
	"voice":       {"rewrite": true, "convert": true, "status": true, "wait": true, "cancel": true, "delete": true, "download": true, "capabilities": true},
	"project":     {"list": true, "create": true, "apply": true, "get": true, "delete": true, "run": true, "wait": true, "stop": true, "rerun": true, "merge": true, "download": true},
}

func (r *Runner) commandNeedsConnection(command string, args []string) bool {
	if explicitHelp(args) {
		return false
	}
	switch command {
	case "doctor":
		return len(args) == 0
	case "capability":
		return (len(args) == 1 && args[0] == "list") || (len(args) == 2 && args[0] == "show" && (args[1] == "video" || args[1] == "image"))
	case "profile":
		return (len(args) == 1 && args[0] == "list") || (len(args) == 2 && args[0] == "show")
	case "douyin":
		return len(args) > 0 && map[string]bool{"import": true, "inspect": true, "capabilities": true, "list": true, "status": true, "wait": true, "cancel": true, "retry": true}[args[0]]
	case "version", "context", "workflow", "completion":
		return false
	case "operation":
		if len(args) == 0 || args[0] == "list" || args[0] == "schema" {
			return false
		}
		if args[0] != "run" {
			return false
		}
		invocation, err := parseOperationRunArgs(args[1:])
		if err != nil || invocation.Name == "asset.copy" {
			return false
		}
		_, exists := operation.Schema(invocation.Name)
		return exists
	default:
		actions, ok := networkCommandActions[command]
		if !ok || len(args) == 0 || !actions[args[0]] {
			return false
		}
		return !(command == "asset" && args[0] == "copy")
	}
}

func (r *Runner) parseGlobals(args []string) (string, []string, error) {
	args, normalizeErr := normalizeGlobalArgs(args)
	if normalizeErr != nil {
		return "", nil, normalizeErr
	}
	set := flag.NewFlagSet("h3ctl", flag.ContinueOnError)
	set.SetOutput(io.Discard)
	set.StringVar(&r.Globals.Context, "context", "", "named connection context")
	set.StringVar(&r.Globals.Server, "server", "", "one-shot MiniMax H3 Video Studio URL")
	set.StringVar(&r.Globals.Format, "output", "table", "table, json, or jsonl")
	jsonOutput := set.Bool("json", false, "alias for --output json")
	rootHelp := set.Bool("help", false, "show help")
	set.DurationVar(&r.Globals.ControlTimeout, "request-timeout", 30*time.Second, "legacy alias for --control-timeout")
	set.DurationVar(&r.Globals.ControlTimeout, "control-timeout", 30*time.Second, "timeout for control-plane HTTP requests")
	set.DurationVar(&r.Globals.TransferTimeout, "transfer-timeout", 0, "upload/download timeout; 0 means unlimited")
	set.DurationVar(&r.Globals.MediaTimeout, "media-timeout", 0, "media derivation timeout; 0 means unlimited")
	set.StringVar(&r.Globals.RequestID, "request-id", "", "idempotency key for generation")
	set.BoolVar(&r.Globals.NonInteractive, "non-interactive", false, "never prompt")
	set.BoolVar(&r.Globals.NoColor, "no-color", false, "disable color")
	set.BoolVar(&r.Globals.Quiet, "quiet", false, "suppress progress on stderr")
	if err := set.Parse(args); err != nil {
		return "", nil, usage("%v", err)
	}
	if *jsonOutput {
		r.Globals.Format = "json"
	}
	if *rootHelp {
		return "--help", nil, nil
	}
	if r.Globals.Format != "table" && r.Globals.Format != "json" && r.Globals.Format != "jsonl" {
		return "", nil, usage("--output must be table, json, or jsonl")
	}
	if r.Globals.ControlTimeout <= 0 || r.Globals.TransferTimeout < 0 || r.Globals.MediaTimeout < 0 {
		return "", nil, usage("control timeout must be positive; transfer/media timeouts may be 0 for unlimited")
	}
	r.Printer = output.Printer{Stdout: r.Streams.Out, Stderr: r.Streams.Err, Format: r.Globals.Format, Quiet: r.Globals.Quiet}
	rest := set.Args()
	if len(rest) == 0 {
		return "", nil, nil
	}
	return rest[0], rest[1:], nil
}

func normalizeGlobalArgs(args []string) ([]string, error) {
	valueFlags := map[string]bool{"--context": true, "--server": true, "--output": true, "--request-timeout": true, "--control-timeout": true, "--transfer-timeout": true, "--media-timeout": true, "--request-id": true}
	boolFlags := map[string]bool{"--json": true, "--non-interactive": true, "--no-color": true, "--quiet": true}
	globals, commands := []string{}, []string{}
	seenGlobal := map[string]bool{}
	canonical := func(name string) string {
		if name == "--request-timeout" {
			return "--control-timeout"
		}
		if name == "--json" {
			return "--output"
		}
		return name
	}
	topCommand, subcommand := "", ""
	for index := 0; index < len(args); index++ {
		arg := args[index]
		if arg == "--" {
			commands = append(commands, args[index:]...)
			break
		}
		if topCommand != "" && commandFlagNeedsValue(topCommand, subcommand, arg) && !strings.Contains(arg, "=") {
			commands = append(commands, arg)
			if index+1 < len(args) {
				index++
				commands = append(commands, args[index])
			}
			continue
		}
		if boolFlags[arg] {
			key := canonical(arg)
			if seenGlobal[key] {
				return nil, usage("global flag %s may only be supplied once", arg)
			}
			seenGlobal[key] = true
			globals = append(globals, arg)
			continue
		}
		if valueFlags[arg] {
			if index+1 >= len(args) || strings.HasPrefix(args[index+1], "--") {
				return nil, usage("%s requires a value", arg)
			}
			if arg == "--server" && topCommand == "context" && (subcommand == "add" || subcommand == "update") {
				commands = append(commands, arg, args[index+1])
				index++
				continue
			}
			key := canonical(arg)
			if seenGlobal[key] {
				return nil, usage("global flag %s may only be supplied once", arg)
			}
			seenGlobal[key] = true
			globals = append(globals, arg, args[index+1])
			index++
			continue
		}
		if topCommand == "context" && (subcommand == "add" || subcommand == "update") && strings.HasPrefix(arg, "--server=") {
			commands = append(commands, arg)
			continue
		}
		matched := false
		for name := range valueFlags {
			if strings.HasPrefix(arg, name+"=") {
				key := canonical(name)
				if seenGlobal[key] {
					return nil, usage("global flag %s may only be supplied once", name)
				}
				seenGlobal[key] = true
				globals = append(globals, arg)
				matched = true
				break
			}
		}
		if !matched {
			commands = append(commands, arg)
			if !strings.HasPrefix(arg, "-") {
				if topCommand == "" {
					topCommand = arg
				} else if subcommand == "" {
					subcommand = arg
				}
			}
		}
	}
	return append(globals, commands...), nil
}

func preparseFormat(args []string) string {
	format, top, sub := "table", "", ""
	for index := 0; index < len(args); index++ {
		arg := args[index]
		if arg == "--" {
			break
		}
		if top != "" && commandFlagNeedsValue(top, sub, arg) && !strings.Contains(arg, "=") {
			index++
			continue
		}
		switch {
		case arg == "--json":
			format = "json"
		case strings.HasPrefix(arg, "--output="):
			value := strings.TrimPrefix(arg, "--output=")
			if value == "json" || value == "jsonl" {
				format = value
			}
		case arg == "--output" && index+1 < len(args):
			value := args[index+1]
			if value == "json" || value == "jsonl" {
				format = value
			}
			index++
		case !strings.HasPrefix(arg, "-"):
			if top == "" {
				top = arg
			} else if sub == "" {
				sub = arg
			}
		}
	}
	return format
}

func commandFlagNeedsValue(top, sub, arg string) bool {
	name := strings.TrimLeft(arg, "-")
	if name == "" || strings.Contains(name, "=") {
		return false
	}
	common := map[string]bool{
		"spec": true, "prompt": true, "prompt-file": true, "profile": true, "aspect-ratio": true,
		"negative-prompt": true, "mode": true, "prompt-mode": true, "first-frame": true, "last-frame": true,
		"source-video": true, "download": true, "width": true, "height": true, "steps": true, "seed": true,
		"duration": true, "cfg": true, "denoise": true, "wait-timeout": true, "poll-interval": true,
		"ref": true, "ref-dir": true, "kind": true, "include": true, "query": true, "folder": true,
		"to": true, "name": true, "to-context": true, "limit": true, "cursor": true, "timeout": true,
		"index": true, "position": true, "at": true, "start": true, "end": true, "segment": true, "input": true,
		"additional-steps": true, "max-short-edge": true, "max-long-edge": true, "max-duration": true,
		"audio": true, "fit": true, "alignment": true, "pad-mode": true, "fps": true, "preset": true,
		"ssh-target": true, "ssh-port": true, "remote-api-port": true,
		"cookies-from-browser": true, "yt-dlp": true, "listen": true, "data-dir": true, "studio-origin": true,
		"cache-ttl": true, "rate-limit": true,
	}
	if name == "server" {
		return (top == "" && sub == "") || (top == "context" && (sub == "add" || sub == "update"))
	}
	return common[name]
}

func (r *Runner) connect(ctx context.Context) error {
	name, selected, err := config.Resolve(r.Store, r.Globals.Context, r.Globals.Server)
	if err != nil {
		return contract.NewError("context_error", err.Error())
	}
	return r.connectResolved(ctx, name, selected)

}

func (r *Runner) connectResolved(ctx context.Context, name string, selected config.Context) error {
	options := r.ConnectionOptions
	options.NonInteractive = r.Globals.NonInteractive
	options.Stderr = r.Streams.Err
	session, err := connection.Open(ctx, selected, options)
	if err != nil {
		return err
	}
	r.Session = session
	r.Service = &operation.Service{API: api.NewWithTimeouts(session.BaseURL, r.Globals.ControlTimeout, r.Globals.TransferTimeout, r.Globals.MediaTimeout), Context: name, PollInterval: 5 * time.Second}
	return nil
}

func (r *Runner) closeConnection() error {
	if r.Session != nil {
		err := r.Session.Close()
		r.Session = nil
		return err
	}
	return nil
}

func mergeCleanupError(primary, cleanup error) error {
	if cleanup == nil {
		return primary
	}
	if primary == nil {
		return cleanup
	}
	cleanupDetail := map[string]any{"code": "ssh_cleanup_failed", "message": cleanup.Error()}
	var cleanupTyped *contract.CLIError
	if errors.As(cleanup, &cleanupTyped) {
		cleanupDetail["code"] = cleanupTyped.Code
		cleanupDetail["details"] = cleanupTyped.Details
	}
	var primaryTyped *contract.CLIError
	if errors.As(primary, &primaryTyped) {
		merged := *primaryTyped
		details := map[string]any{"cleanup_error": cleanupDetail}
		if existing, ok := primaryTyped.Details.(map[string]any); ok {
			for key, value := range existing {
				details[key] = value
			}
		} else if primaryTyped.Details != nil {
			details["primary_details"] = primaryTyped.Details
		}
		merged.Details = details
		merged.Cause = errors.Join(primary, cleanup)
		return &merged
	}
	return &contract.CLIError{Code: "internal_error", Message: primary.Error(), Details: map[string]any{"cleanup_error": cleanupDetail}, Cause: errors.Join(primary, cleanup)}
}

func usage(format string, args ...any) error {
	return &contract.CLIError{Code: "usage", Message: fmt.Sprintf(format, args...)}
}
func commandPath(top string, args []string) string {
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		return top + "." + args[0]
	}
	return top
}

const RootHelp = `h3ctl controls MiniMax H3 Video Studio for people and automation agents.

Usage:
  h3ctl [global flags] COMMAND [SUBCOMMAND] [flags]

Commands:
  version       Print CLI version
  doctor        Check API and generation capabilities
  context       Manage direct or temporary-SSH connections
  capability    Inspect server capabilities
  profile       Inspect versioned generation profiles
  asset         Upload, download, copy, inspect, update, pin, or delete assets
  generate      Generate an image or video
  job           Inspect, wait for, cancel, download, save, or delete jobs
  media         Extract, trim, remove, or atomically replace media audio
  video         Compose, migrate characters, trim, or concatenate video
  voice         Convert speech or singing timbre with GPU-safe workers
  replication   Plan, review, edit and resume replication projects
  project       Manage long-video projects
  operation     Discover and invoke stable Agent operations
  douyin        Import Douyin into Studio; local download and task management
  workflow      Reserved for resumable operation DAGs
  completion    Reserved for shell completion

Global flags:
  --context NAME          Named context (default: current, env, then local)
  --server URL            One-shot direct server override
  --output table|json|jsonl
  --json                  Alias for --output json
  --request-timeout 30s   Legacy control-plane alias; never transfer/media
  --control-timeout 30s   Control-plane timeout (--request-timeout alias)
  --transfer-timeout 0    Upload/download timeout; 0 is unlimited
  --media-timeout 0       Derivation timeout; 0 is unlimited
  --request-id ID         Idempotency key for generation requests
  --non-interactive       Never prompt
  --no-color              Disable color
  --quiet                 Suppress progress on stderr

Environment:
  H3_STUDIO_URL, H3CTL_CONFIG, H3CTL_YTDLP

Resource locators:
  ./file.png | file:///abs/file.png | asset:ID | job:ID#INDEX | media:ID
  h3://CONTEXT/assets/ID

Run h3ctl COMMAND --help for command examples and flags.
JSON/JSONL stdout is protocol-only; progress and diagnostics go to stderr.

Example:
  h3ctl --context dev --json doctor
`

const WorkflowHelp = `Usage: h3ctl workflow validate|plan|run|status|resume

The command namespace and operation contracts are reserved for a future
resumable DAG runner. V1 deliberately returns unsupported for execution; it
never pretends a workflow was accepted. Use operation schema/run for atomic
steps today.
Defaults: no workflow action flags are implemented in v1.
Example: h3ctl workflow run --help
`

func (r *Runner) runWorkflow(args []string) (any, error) {
	if help(args) {
		fmt.Fprint(r.Streams.Out, WorkflowHelp)
		return nil, nil
	}
	switch args[0] {
	case "validate", "plan", "run", "status", "resume":
		return nil, contract.Unsupported("workflow "+args[0], "the resumable DAG engine is reserved for a later CLI version; use operation run")
	default:
		return nil, usage("unknown workflow command %q", args[0])
	}
}
