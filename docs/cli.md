# MiniMax H3 Video Studio Go CLI (`h3ctl`)

`h3ctl` is the scriptable client for the existing MiniMax H3 Video Studio Python API. It does not compile ComfyUI graphs or read server storage directly. Generation continues on the server when the CLI exits; reconnect with `job wait` using the returned job ID.

## Build

```bash
cd cli
go build -o h3ctl ./cmd/h3ctl
./h3ctl --help
```

The CLI defaults to `http://127.0.0.1:6020`. A one-shot direct connection can use `--server`. Named contexts support either a direct URL or a temporary SSH tunnel:

```bash
h3ctl context add local --server http://127.0.0.1:6020 --use
h3ctl context add dev --ssh-target h3-dev --remote-api-port 6020
h3ctl context test dev
```

An SSH context stores only its target, optional SSH port, and remote API port. Each API command starts a temporary local forward to remote `127.0.0.1:6020`, waits until it is ready, keeps it alive for the entire command (including a long `job wait`), then closes and reaps it. `context list`, `show`, `add`, `update`, `use`, and `remove` never start SSH; `context test` does.

Use an SSH config alias so a rented machine can change addresses without changing the H3 context:

```sshconfig
Host h3-dev
  HostName gpu.example.com
  User your-user
  Port 2222
  IdentityFile ~/.ssh/id_ed25519
```

After an address or port change, edit only `HostName`/`Port` above. Alternatively update stored connection settings with `h3ctl context update dev --ssh-target new-alias --ssh-port 12900`, then run `h3ctl context test dev`. Use `h3ctl context update dev --clear-ssh-port` to return to the `Port` selected by SSH config. An update with no connection flags is rejected, and `context add` never overwrites an existing name.

Passwords and identity files are never stored by `h3ctl`; standard SSH config, public-key authentication, and `ssh-agent` are recommended. SSH is always started with `-n`, so command JSON from `--spec -` or `--input -` remains exclusively owned by `h3ctl`; `--non-interactive` additionally adds `BatchMode=yes`, which is appropriate for Agents and fails instead of prompting for a password. `H3_STUDIO_URL` and `H3CTL_CONFIG` remain available for direct/default configuration.

Connection startup uses a private temporary SSH ControlMaster socket. The master still reads the named host from your SSH config (including HostName, User, IdentityFile, ProxyJump, and Port), but `h3ctl` uses `-N`/`SessionType=none` and command-line overrides to disable remote sessions, backgrounding, TTY allocation, and inherited forwards for this session. Its private check/forward/exit control calls ignore user SSH config and request exactly one CLI-owned local forward. `h3ctl` first checks that the authenticated master is alive, then asks that exact master to bind the local forward; a conflicting local port is reallocated and retried before any health request. Only after the forward command succeeds does it validate the strict H3 `/health` JSON contract, including an exact JSON content type and a 64 KiB response limit. Both normal (`200`, ComfyUI healthy) and H3 degraded (`503`) contracts are accepted, while redirects, unrelated listeners, trailing JSON/content, and wrong content types are rejected. Tunnel termination, process reaping, and private-directory cleanup are bounded and complete before a success envelope is printed. Cleanup failures return `ssh_cleanup_failed`; Ctrl-C during forwarding returns `interrupted`, and a forwarding startup deadline returns `ssh_start_timeout`. Platforms or SSH builds without ControlMaster support return an explicit SSH control/start failure instead of falling back to an ambiguous listener probe.

## Agent output contract

```bash
h3ctl --output json capability list
h3ctl job wait job:ID --output jsonl --timeout 2h
h3ctl operation schema generate.video --json
```

JSON commands emit one `h3ctl.output/v1` envelope to stdout. JSONL wait progress is emitted to stdout as event objects, beginning with `submitted` when generation has been accepted; human progress and diagnostics use stderr. Table mode renders scalar columns or key/value rows. Errors have stable `code`, `message`, `retryable`, optional HTTP `status`, and optional `details` fields.

Help, unknown command/subcommand handling, `workflow`, `completion`, `operation list`, and `operation schema` are local operations. They do not start the current SSH context. `asset copy` and `operation run asset.copy` open only their declared source and destination contexts.

Help is intentionally plaintext, including when `--json` or `--output jsonl` is present. `--` ends global flag extraction; every following token belongs to the selected command.

Exit codes are: `2` invalid usage, `3` authentication/authorization, `4` missing resource, `5` wait timeout, `6` failed generation, `7` cancelled generation, and `8` unsupported server contract. Other operational failures return `1`.

`--control-timeout` (legacy alias `--request-timeout`) defaults to 30 seconds for health, status, metadata and submission requests. `--transfer-timeout` and `--media-timeout` default to `0` (unlimited) so a large upload/download or ffmpeg derivation is not accidentally killed by the control-plane default. `job wait --timeout` and `generate ... --wait-timeout` separately limit the complete wait. Negative timeouts are rejected. `Ctrl-C` stops the local wait only; it never cancels the remote job. Cancellation requires `job cancel`.

Redirects are never followed, so requests cannot silently move to a different origin. Generation submission uses the same payload and `request_id` to retry an ambiguous network disconnect; if recovery is exhausted, the error includes that `request_id`. After acceptance, wait or download errors include the `job_id`, `request_id`, and submission receipt needed to resume safely.

## Local Douyin parsing and download

`h3ctl douyin` is a local utility and is independent of the H3 Studio server. It
never opens the selected direct or SSH context. Install
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) on the machine running `h3ctl`, or
provide its executable with `--yt-dlp PATH` / `H3CTL_YTDLP`:

```bash
# Parse a copied share message or a public Douyin HTTPS URL.
h3ctl douyin parse \
  '1.71 复制打开抖音 https://v.douyin.com/...' \
  --cookies-from-browser chrome --json

# Download to a directory. yt-dlp chooses the media extension.
h3ctl douyin download 'https://v.douyin.com/...' \
  --to ./downloads/ --cookies-from-browser chrome --json
```

Only exact public `douyin.com` and `iesdouyin.com` HTTPS hosts are accepted;
userinfo, explicit ports, other schemes and look-alike hosts are rejected before
starting `yt-dlp`. Browser cookies are opt-in: `h3ctl` passes the
`--cookies-from-browser` value directly to `yt-dlp`, does not export a cookie
file, and does not persist cookie contents. If Douyin invalidates the session,
open `douyin.com` in that browser, complete any login/challenge there, then run
the command again. Do not automate password entry or distribute browser cookie
databases.

The download command does not overwrite an exact output file unless `--force`
is supplied. Directory output uses a sanitized uploader/video-ID template. It
returns the absolute file path, size and SHA-256 digest. Use this capability
only for media you own or are authorized to download; it does not remove a logo
that is already embedded in the video pixels.

If Douyin reports that fresh cookies are needed, first stop repeated attempts
and check whether the video plays in the same Chrome profile. That error alone
does **not** prove the login expired: yt-dlp tracks cases where Douyin playback
works but extraction still fails ([upstream issue](https://github.com/yt-dlp/yt-dlp/issues/9667)).
After confirming playback, try once later. If extraction remains blocked, upload
an authorized local video through Studio instead. Do not share browser cookies
or use repeated automated retries as a recovery method.
CLI and loopback task errors keep stable diagnostic codes: `cookie_refresh_required`
means the request was rejected, not that the browser login is definitely expired;
`access_restricted` identifies HTTP 403 and `rate_limited` identifies HTTP 429.
Studio displays the failing stage, a plain-language explanation and the next
step. It clears the previous progress message when a local import fails, so an
old “downloading” status is not mistaken for an active task.

### Loopback Swagger API

Run the same extractor as a local asynchronous API:

```bash
h3ctl douyin serve \
  --listen 127.0.0.1:8765 \
  --cookies-from-browser chrome

open http://127.0.0.1:8765/docs
```

The server exposes:

- `POST /api/parse` with `{"text":"share text or URL"}`;
- `POST /api/inspect` with the same body to return metadata without downloading;
- `GET /api/tasks/{id}` for `pending`, `running`, `completed` or `failed`;
- `GET /api/download/{token}` for an expiring, Range-capable media download;
- `GET /openapi.json`, `GET /docs` and `GET /health`.

It is deliberately restricted to loopback addresses. The default cache TTL is
one hour, the default request limit is 30 submissions per client IP per minute,
and at most two extractor tasks run concurrently. Duplicate URLs reuse a live
or unexpired task. Retryable failures are cached for at most one minute so a
refreshed browser session can be tried later without immediate request loops.
Cached files live under the platform user cache directory
(`h3ctl/douyin`) unless `--data-dir` is set; expired managed files are removed.
Task responses never reveal their server-side filesystem path. Swagger UI loads
its static assets from the public `unpkg.com` CDN, while the API and OpenAPI
document themselves remain local.

### Studio browser import on macOS

For a Studio opened through the local SSH tunnel at `http://127.0.0.1:16020`,
start the local helper on the Mac running Chrome:

```bash
./scripts/douyin-helper-macos.sh install
./scripts/douyin-helper-macos.sh status
```

The installer builds the current `h3ctl` and uses an existing `yt-dlp` or
installs it in a user-local environment when `uv` is available. It starts a
session helper on `127.0.0.1:8765`, allowing only the exact loopback Studio
origin. Run `install` again after logging into macOS. Studio discovers it
automatically; its Douyin
drawer uses local Chrome cookies for **Parse** and **Import**, then uploads only
video bytes through the existing asset API. The browser never receives the
Cookie database or account credentials. The helper does not read Chrome cookies
until the user requests a parse or import. The allowed origin can be changed
at install time with `H3_STUDIO_LOCAL_ORIGIN=http://127.0.0.1:PORT`.

macOS denies a normal LaunchAgent access to Chrome's Cookie database, even
when the same CLI can read it from the user's terminal. For automatic startup
at login, first grant Full Disk Access to the installed `h3ctl` and `yt-dlp`
executables in macOS System Settings, then run
`./scripts/douyin-helper-macos.sh install-login`. Without that one-time system
permission, use the session helper above. Neither mode exports cookies to the
development machine.

To stop and remove it, run `./scripts/douyin-helper-macos.sh uninstall`.
This integration uses third-party automated requests and may trigger Douyin
account restrictions. Use it only for videos you are allowed to download.

## Resource locators and transfers

Commands consistently accept:

```text
./frame.png
file:///absolute/frame.png
asset:ASSET_ID
job:JOB_ID#OUTPUT_INDEX
media:DERIVATION_ID
h3://CONTEXT/assets/ASSET_ID
```

Local generation inputs are uploaded first. Job and derivation inputs are materialized as internal server assets and do not appear in the user library until explicitly saved. A locator from another context must first be copied with `asset copy`; cross-machine transfers stream through an isolated local temporary file. Downloads use a unique same-directory `.part` file and an atomic commit. Non-force commits are atomic no-replace; `--force` atomically replaces only after a complete download, so an interrupted transfer preserves the old destination.

`file:` locators accept an empty authority or `localhost`; `file://localhost/path` means the local machine running `h3ctl`. Other file authorities are rejected instead of being mapped to local storage. Locator userinfo, query strings, and fragments are rejected (except the documented `job:ID#INDEX` output selector), and validation errors do not echo secret-bearing URIs. All server-created asset, job, derivation, project, and segment IDs are exactly 32 lowercase hexadecimal characters.

Directory upload is explicit and deterministic. Recursive traversal rejects symbolic links rather than following them:

```bash
h3ctl asset upload ./references --recursive \
  --include '*.png' --include '*.mp4' --output json
```

## Generation

Image:

```bash
h3ctl generate image \
  --prompt-file prompt.txt \
  --ref ./character.png \
  --profile auto --aspect-ratio 3:4 \
  --wait --download ./output.png
```

Qwen-Image 2.1 uses one BF16 profile for both text-to-image and ordered
instruction edits. Specify it explicitly so an Agent does not silently use
another available image model. It requires a recent ComfyUI with the native
Qwen 2.1 nodes and the three BF16 model files; check `h3ctl profile show
qwen-image-2.1-bf16` first. The upstream weights use the non-commercial
[Qwen Research License](https://github.com/QwenLM/Qwen-Image-2.1/blob/main/LICENSE).

```bash
# No reference: native 2K text-to-image. Omit --seed for a random seed.
h3ctl generate image --profile qwen-image-2.1-bf16 \
  --prompt 'A blue ceramic teapot on a white table, product photograph' \
  --width 2048 --height 2048 --steps 40 --cfg 1 --seed 42 \
  --wait --download ./teapot.png

# Reference order is meaningful; the first image establishes the edit canvas.
h3ctl generate image --profile qwen-image-2.1-bf16 \
  --ref ./subject.png --ref ./palette.png \
  --prompt 'Preserve the subject of <image1>; apply the palette of <image2>.' \
  --width 2048 --height 2048 --steps 40 --cfg 1 --seed 43 \
  --wait --download ./edited.png
```

This profile accepts 1–10 images for editing, supports explicit steps, CFG,
negative prompt, seed and native 2K dimensions, and does not support `denoise`.
The CLI's `generate.image` Agent operation shares the same profile and ordered
references. See [Image Workflows](image-workflows.md) for the exact graph,
capability gate and license boundary.

Video modes are explicit:

```bash
h3ctl generate video \
  --mode fl2v \
  --first-frame ./first.png \
  --last-frame asset:LAST_FRAME_ID \
  --prompt-file prompt.txt \
  --duration 10 \
  --wait --wait-timeout 2h \
  --download ./shot.mp4
```

Supported modes are `t2v`, `i2v`, `fl2v`, `r2v`, `v2v`, and `rv2v`. A bare `--ref LOCATOR` is always treated literally, so filenames and URIs containing `=` or `,` work without escaping. Structured roles use the explicit `json:` prefix, for example `--ref 'json:{"role":"identity","source":"asset:ID"}'`. `--ref-dir 'json:{"role":"reference","path":"./dir,with,commas"}'` is the recommended unambiguous directory form; legacy `role=reference,path=...` remains supported when the path contains no comma. Directory expansion uses stable order and enforces the H3 Ref2VA limits before submission: at most 9 images, 3 videos, 3 audio files, and 12 mixed files in total. All three video entry points—typed flags, `--spec`, and `operation run generate.video`—default to `prompt_mode=preserve_tags_only`.

By default generation only submits and returns the durable job ID:

```bash
h3ctl generate video --mode t2v --prompt 'A sunrise' --json
h3ctl job wait job:ID --timeout 0 --output jsonl
h3ctl job download job:ID --to ./sunrise.mp4
```

## End-to-end long-video composition

`video compose` is the high-level CLI entry point. It keeps the existing
project lifecycle atomic and reusable while providing a single command for
create, ordered generation, Motion Context head trim, validated concat, and
final atomic download:

```bash
h3ctl video compose --spec ./trilogy.json --to ./final.mp4 --timeout 0
```

The input is the same object accepted by `POST /api/video-projects`. Explicit
Profile IDs may omit `profile_version` and `profile_digest`; `video compose`
pins both from current capabilities before project creation. Agent callers can
invoke the same path as operation `video.compose`.

The atomic aliases remain available:

```bash
h3ctl video trim asset:ID --start 1 --end 5
h3ctl video concat PROJECT_ID
h3ctl project wait PROJECT_ID --timeout 0
h3ctl project download PROJECT_ID --to ./final.mp4
```

Turbo4 step counts remain user-configurable within Profile limits. A later
segment selects latent continuation with `continuation: "motion_context"` and
optional `motion_context.video_frames` / `audio_frames`. See
[Motion Context long-video composition](motion-context-long-video.md) for the
full contract, pinned external node version, recovery, storage, and dimension
rules.

## Replication workshop

`video replicate` turns a source of any positive duration (at least one 24 FPS frame) into a durable native H3
project. It preserves selected timing/motion/camera properties, applies the
requested replacements and optional image references, splits the source into
legal `17k+5` H3 windows, then merges and trims the result to the exact source
frame count:
There is no fixed source-duration limit. Short sources are padded only in private model inputs and trimmed back after merging. Upload size, disk space, and `H3_STUDIO_MAX_PROJECT_JSON_BYTES` still apply; planning reports an explicit capacity error before generation if the project would exceed its JSON budget.

```bash
h3ctl video replicate \
  --source ./source.mp4 \
  --brief "Keep the performance and camera; replace the presenter and product" \
  --reference ./presenter.png --reference ./product.png \
  --replace-product "H3 Studio" \
  --audio copy-source --continuity auto \
  --to ./replicated.mp4 --timeout 0
```

Use `--plan-only` to inspect the compiled `h3.replication/v1` recipe and
project without submitting generation. `--detach` creates and starts the
project, then returns its ID for recovery through `h3ctl project`. Specs use
the same fields as Agent operations `video.replication.plan` and
`video.replication.produce`. Douyin fetching remains local and separate: run
`h3ctl douyin download URL` first, then pass the downloaded file as `--source`.

### Reviewable CLI workflow

Use `replication` for work that needs human or Agent review between planning and generation:

```bash
# Resolves/uploads inputs, but does not create or run a project.
h3ctl replication plan --source ./source.mp4 --brief "Keep motion; replace the product" --json > plan.json
# Accepts the plan, an exported project spec, or either inside the CLI JSON envelope.
h3ctl replication create --plan plan.json --json
h3ctl replication list --json
h3ctl replication inspect PROJECT_ID --json
# Prompt file contents are submitted without rewriting. Save the timestamp returned by inspect
# when preparing edits asynchronously, then pass it to protect against concurrent changes.
h3ctl replication edit-segment PROJECT_ID --segment SEGMENT_ID --prompt-file shot.txt \
  --expected-updated-at 1790000000.125
h3ctl replication run PROJECT_ID
h3ctl replication wait PROJECT_ID --timeout 0
h3ctl replication resume PROJECT_ID --to ./final.mp4
h3ctl replication export PROJECT_ID --json > reviewed-project.json
```

`create` saves a draft without consuming generation resources. `edit-segment` accepts
`--prompt-file`, `--steps`, and/or `--seed`; changing a segment invalidates its result,
dependent continuation segments, and the merged output. Independent completed segments
remain reusable. If `--expected-updated-at` is omitted, the CLI reads the current version
immediately before patching; pass the version from your original `inspect` for edits prepared
earlier. A stale version returns HTTP 409 `project_changed`. Active projects cannot be edited.

`resume` uses the existing project ID, runs unfinished segments, waits, merges if needed,
and downloads atomically. A completed merged result is downloaded directly. It never creates
a replacement project. Ctrl-C stops local waiting only; `stop PROJECT_ID` requests server-side
stop. `run`, `wait`, `stop`, `rerun --segment`, `merge`, and `download` use the shared project
commands and their existing flags. Export omits execution results; importing it creates a
new draft using assets on the same server, not a portable media archive.

Agent operations are `video.replication.create`, `.inspect`, `.export`, `.edit_segment`, and
`.resume`, in addition to `.plan` and `.produce`. Discover exact input contracts with
`h3ctl operation schema video.replication.edit_segment --json`. The edit operation always
requires `expected_updated_at`. Generic execution primitives remain `project.*`.

The browser detects scene cuts before planning; CLI plans use legal balanced windows unless
`cut_frames` is supplied in `--spec`. Neither route performs semantic video understanding,
transcription, word-level captions, or a Hypit-style composition renderer. The recipe's
`prompt_policy.prompt_sha256` records the initial compiled prompt; edited segment requests
are the authoritative prompts used for execution. See [workflow design](replication-workflow.md).

## Unlimited-duration character migration

`video migrate-character` replaces one clearly identified source performer
with one character image while preserving the source motion, timing, camera,
framing, scene, lighting, composition, and interactions. Local paths and all
normal asset locators are accepted:

For an Agent-ready end-to-end workflow, including plan review, stable prompt
aliases, recovery, and final media delivery, use the bundled
[`h3-character-migration` Skill](../skills/h3-character-migration/SKILL.md).

```bash
h3ctl video migrate-character \
  --source ./performance.mp4 \
  --character ./hero.png \
  --source-subject "the centered dancer wearing red" \
  --profile minimax-h3-ref2va \
  --steps 4 --lora-strength 1 \
  --segment-frames 243 --overlap-frames 39 \
  --audio copy-source \
  --to ./migrated.mp4 --timeout 0
```

The source is normalized once to a 24 FPS planning timeline. Legal generation
windows use `17k+5` frames from 124 through 362; overlap is 5, 22, 39, or 56
frames. The first window is independent and each later window carries the
matching video/audio overlap through Motion Context. The terminal window first
shifts backward and selects the largest supported overlap that still covers all
remaining source frames. Only a sub-grid remainder or a source shorter than the
minimum H3 window is padded in its private model input. Merge owns the first
segment in full, removes each segment's actual overlap before concatenation,
then applies only any unavoidable final grid trim to reach the exact source
frame count.

Turbo defaults to four steps but accepts any step count allowed by the selected
Profile; Base Profiles are LoRA-free and require `lora_strength=0`. Audio modes
are:

- `copy-source` (default): replace final generated audio with the exact-length source audio;
- `reference-source`: provide each range's aligned source audio to H3 and keep generated audio;
- `generate`: do not reference source audio and keep generated audio;
- `mute`: remove final audio.

`copy-source` and `reference-source` fail during planning if the source has no
usable audio. `h3ctl media mux-audio VIDEO AUDIO` exposes the same safe atomic
pad/trim mux primitive for general use.

Use `--plan-only` to inspect source ranges, ownership, final trim, profile pin,
prompt bindings, and storage estimates without creating a project or spending
GPU work. `--detach` creates and starts the durable project, then returns its
`project_id`; `project get`, `project run`, `project wait`, `project merge`, and
`project download` resume it without regenerating completed segments. Ctrl-C
also leaves the remote project running and reports its ID in the structured
error details.

The versioned JSON form is accepted with `--spec PATH|-`:

```json
{
  "version": "h3.character-migration/v1",
  "source": "asset:SOURCE_ID",
  "targets": [
    {
      "character": "asset:CHARACTER_ID",
      "source_subject": "the centered dancer wearing red",
      "details": "Keep the character's facial features and clothing design stable"
    }
  ],
  "profile_id": "minimax-h3-ref2va",
  "steps": 4,
  "lora_strength": 1,
  "segment_frames": 243,
  "overlap_frames": 39,
  "audio_policy": "copy-source"
}
```

Agent callers use strict Draft 2020-12 operations
`video.character_migration.plan`, `video.character_migration.produce`, and
`media.mux_audio`. Unknown top-level and nested fields are rejected. The server
capability `video.character_migration` reports availability, schema/recipe
version, profiles, frame grids, audio policies, and Motion Context status.

## Voice conversion

Voice conversion is also available in the Studio sidebar, including browser
microphone recording for either the source or reference. The CLI/Agent commands below upload local
inputs as audio assets and return a durable task ID. Conversion waits by
default; use `--detach` to submit without keeping the CLI connected.

```bash
# Speech or singing: Amphion Vevo2 FM-only, style-preserved VC/SVC.
h3ctl voice convert ./source.wav \
  --reference ./reference.wav --engine vevo2 --to ./converted.wav

# Song: YingMusic separation -> lead-vocal SVC -> accompaniment remix.
h3ctl voice convert ./song.wav \
  --reference asset:REFERENCE_ID --engine yingmusic \
  --steps 100 --cfg 0.7 --seed -1 --detach --json
h3ctl voice status TASK_ID --json
h3ctl voice wait TASK_ID --timeout 2h --output jsonl
h3ctl voice download TASK_ID --to ./converted-song.wav
# Optional diagnostic tracks and effect switches for a new YingMusic task:
h3ctl voice convert ./song.wav --reference asset:REFERENCE_ID --engine yingmusic \
  --keep-stems --echo=false --reverb=false --detach
h3ctl voice download TASK_ID --track dry_vocal --to ./dry-vocal.wav
h3ctl voice download TASK_ID --track accompaniment --to ./accompaniment.wav
```

Voice inputs pass content-signature and ffprobe validation. The currently
accepted formats are MP3, WAV, FLAC, and OGG; changing only a filename suffix
does not bypass validation. Both engines deliver lossless WAV
(`audio/wav`, `converted.wav`) to avoid an extra lossy encode before later
mixing or editing.

For YingMusic, `--steps` accepts 10–200 (default 100), `--cfg` accepts 0–2
(default 0.7), and `--seed` accepts -1 or 0–4294967295 (default -1). `-1`
generates a fresh seed for each task; the task receipt includes the effective
seed under `parameters`, and `voice status` can retrieve it later. Reuse that
seed for another draw with the same settings. The upstream 100-step setting is
a quality/speed balance, not a universal best result. GPU inference may not
be bit-for-bit deterministic. `voice.convert` accepts the API field names
`diffusion_steps`, `inference_cfg_rate`, and `seed` for YingMusic only.
`--keep-stems` retains the dry converted vocal and separated accompaniment;
without it only the final mix is saved. `--echo=false` and `--reverb=false`
disable those effects on the final mix without altering the dry vocal or
accompaniment. Both effects remain enabled by default for compatibility.
`voice download --track` accepts `mix` (default), `dry_vocal`, or
`accompaniment`; the same persistent WAV bytes back the browser preview and
the CLI download. Agent `voice.convert` accepts `output_options` with boolean
`include_stems`, `echo`, and `reverb`, and `voice.download` accepts `track`.
Existing tasks still expose their original final mix only.

`voice cancel`, `voice delete`, `voice capabilities`, and the Agent operations
`voice.convert|get|wait|cancel|delete|download` are also available. A local Ctrl-C
only stops waiting; use `voice cancel` for remote cancellation. Check
`h3ctl voice capabilities` before submitting: an unavailable engine reports
the missing runtime/checkpoint without falling back to another model.

All video/image generation and voice tasks share the server's exclusive FIFO
GPU queue. `printf '{}\n' | h3ctl operation run gpu.status --input - --json` exposes
memory telemetry, the active owner, resident model, queue positions and wait
reasons. There is no force/preempt option: model switches happen only after the
active lease finishes or is explicitly canceled.

Explicit profile IDs are resolved against `/api/capabilities`, and their current `profile_version` and `manifest_sha256` are submitted automatically. A generation `request_id` is always supplied for idempotency; pass `--request-id` to reuse one across submission retries.

## Frames and other atomic media operations

```bash
h3ctl media frame job:ID#0 --position first
h3ctl media frame job:ID#0 --position current --at 3.5
h3ctl media endpoints job:ID#0
h3ctl media trim asset:ID --start 1 --end 5
h3ctl media extract-audio asset:ID
h3ctl media remove-audio asset:ID
h3ctl media prepare-reference ./large-reference.mp4 --preset h3-low-token
h3ctl media prepare-reference asset:ID --audio keep --max-duration 15
```

These call `/api/media/derive` and return derivation receipts. Use `media save` to promote a receipt into the asset library, or `media download` to download it locally.

`media prepare-reference` submits ffmpeg work as a durable background media task on the MiniMax H3 Video Studio server, polls its status, and emits `media_submitted` / `media_progress` JSONL events (or human progress on stderr). Pressing Ctrl-C cancels this remote preprocessing task; completed receipts survive client disconnects and can be recovered from the server. It preserves display orientation and aspect ratio, limits the H3 reference canvas to 480/864 edges, aligns it to 32 pixels, produces 24 FPS H.264/YUV420P, and never overwrites the source. Without `--preset h3-low-token`, `--audio keep|remove` is required explicitly. The returned `media:ID` can be passed directly to generation or saved to the asset library.

On `sm120 + SageAttention`, the server also evaluates the complete Ref2VA packed sequence before submission. When the configured versioned policy is exceeded, it creates the same controlled derivation automatically and records the original/derived mapping in the job receipt; target resolution, duration, steps, model, and prompt remain unchanged. A preprocessing failure prevents generation submission and returns its stage, request ID, and already-materialized locators.

## Resumable Base sampling

The default Base Profiles are Direct Profiles: `minimax-h3-fl2va-base` and
`minimax-h3-ref2va-base` render the requested video without putting checkpoint
I/O on the critical path. They intentionally advertise `resume.supported=false`.

Continuation is opt-in through `minimax-h3-fl2va-base-resumable` and
`minimax-h3-ref2va-base-resumable`. These Profiles require the bundled
`H3StudioSaveLatent` / `H3StudioLoadLatent` ComfyUI nodes. The save node supports
H3's video/audio `NestedTensor`, runs after `SaveVideo`, and is best effort: a
checkpoint write failure leaves the video job completed and reports
`checkpoint_error` / `can_resume=false`. Turbo LoRA Profiles remain unsupported
until their behavior beyond the tested schedule is validated.

```bash
h3ctl generate video --profile minimax-h3-fl2va-base-resumable --steps 7 \
  --mode t2v --prompt 'A slow cinematic sunrise'

h3ctl job resume job:ID --additional-steps 3
h3ctl job resume job:ID --additional-steps 3 \
  --wait --poll-interval 5s --download ./continued.mp4
```

The server resolves any task ID in a continuation chain to its latest valid checkpoint. A resume creates a new immutable result, executes only the requested new sigma segment, and replaces the chain checkpoint only after the new latent is stored and verified. The prior result and checkpoint survive failure or cancellation. Profile identity, model, LoRA state, prompt, seed, sampling settings, shape, and reference hashes are validated; a mismatch fails explicitly and never falls back to regeneration. Operators install `comfy_nodes/h3_studio_checkpoint` as `ComfyUI/custom_nodes/h3_studio_checkpoint` and restart ComfyUI before enabling the resumable Profiles.

Checkpoint retention is configured server-side (24–72 hours, default 48). Job list and status receipts expose current/max steps, latest task, expiry, `can_resume`, and a truthful unavailable reason. Only one continuation may run per chain.

## Atomic operations and future workflows

The human command tree and future workflow runner share reusable Go operations. Agents can discover them without scraping help text:

```bash
h3ctl operation list --json
h3ctl operation schema media.frame --json
h3ctl operation run media.frame --input request.json --json
```

`operation run` validates required fields, primitive types, enums, unknown fields, and integer values against the same schema returned by `operation schema`; it does not truncate numbers. Generation operation references accept the same local/file/asset/job/media locators as typed generation. `workflow` is reserved in v1 and returns an explicit `unsupported` error. A later workflow DAG engine can call the same operation service objects directly instead of spawning nested CLI processes. The raw ComfyUI graph for a completed generation remains available separately through `job workflow`.

## Current API boundaries

Commands map only to real server endpoints. Asset rename/folder/pin, job lifecycle, derivations, and long-video projects are supported. Operations for which the Python API has no contract return `unsupported`; the CLI never reports fabricated success. Shell completion and the resumable workflow runner are intentionally reserved for a later release.

## Douyin imports in Studio

The Studio sidebar **Douyin / 抖音** parses share text, downloads on the server,
imports the video into the common asset library, and opens it in the replication
workshop. Tasks persist across page reloads. Interrupted server tasks become
retryable failures; finished assets remain available. Quality presets are best,
up to 1080p, and up to 720p. Cancel is available until asset import begins.

```bash
h3ctl douyin capabilities
h3ctl douyin inspect 'https://v.douyin.com/...' --json
h3ctl douyin import 'https://v.douyin.com/...' --quality 1080 --detach --json
h3ctl douyin list --json
h3ctl douyin status TASK --json
h3ctl douyin wait TASK --timeout 10m --json
h3ctl douyin cancel TASK --json
h3ctl douyin retry TASK --json
# Local browser session: download locally, then upload to selected Studio context.
h3ctl douyin import 'https://v.douyin.com/...' --local --cookies-from-browser chrome
```

These new commands use the selected direct/SSH context. Existing `parse`,
`download`, and `serve` retain their local-only behavior. `import --local` reads
browser cookies only when explicitly selected and uploads video bytes, not the
cookie database. It currently uses best quality and waits for upload completion.
`Ctrl-C` during remote waiting does not cancel the server task. Use `cancel`.
`--request-id` gives remote submissions a stable idempotency key.

Agent operations: `douyin.submit` (`text`, optional `mode=parse|download`,
`quality=best|1080|720`, `request_id`), `douyin.capabilities`, `douyin.list`,
`douyin.get`, `douyin.wait`, `douyin.cancel`, and `douyin.retry`.

Server routes: `GET /api/douyin/capabilities`, `GET/POST /api/douyin/tasks`,
`GET /api/douyin/tasks/:id`, `POST /api/douyin/tasks/:id/cancel|retry` (empty JSON).
All use existing API authentication and browser Origin checks. Receipts expose
metadata and an asset ID, never signed media URLs, cookie contents or disk paths.
At most two yt-dlp processes run concurrently with eight active/queued tasks.
The task list returns the newest 100 receipts. Downloads are size/time bounded;
assets use existing byte deduplication, validation, 24 FPS normalization and quota.

Install yt-dlp and FFmpeg on the Studio server. `H3_STUDIO_YTDLP` selects the
executable. `H3_STUDIO_DOUYIN_COOKIES` optionally points to an operator-managed
Netscape cookie file outside the release directory (restrict access to its owner).
Each task uses a disposable private copy. A rejected request may be reported as
`cookie_refresh_required`, `access_restricted` (HTTP 403) or `rate_limited`
(HTTP 429). These codes do not prove the login expired; check browser playback
and avoid consecutive retries. Neither site support nor cookie configuration
guarantees that Douyin will accept a request.
