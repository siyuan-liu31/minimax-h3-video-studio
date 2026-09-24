#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This helper installer supports macOS only." >&2
  exit 2
fi

action="${1:-install}"
label="com.h3studio.douyin-helper"
agent="$HOME/Library/LaunchAgents/$label.plist"
support="$HOME/Library/Application Support/H3Studio/douyin-helper"
session_pid="$support/session.pid"
logs="$HOME/Library/Logs/H3Studio"
userbin="$HOME/.local/bin"
uid="$(id -u)"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$action" == "uninstall" ]]; then
  launchctl bootout "gui/$uid" "$agent" 2>/dev/null || true
  if [[ -f "$session_pid" ]]; then
    pid="$(cat "$session_pid")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && ps -p "$pid" -o command= | grep -Fq "$support/h3ctl douyin serve"; then
      kill "$pid"
    fi
    rm "$session_pid"
  fi
  rm -f "$agent"
  for name in h3ctl yt-dlp; do
    if [[ -L "$userbin/$name" && "$(readlink "$userbin/$name")" == "$support/"* ]]; then
      rm -f "$userbin/$name"
    fi
  done
  echo "Douyin local helper stopped and removed."
  exit 0
fi
if [[ "$action" == "status" ]]; then
  if [[ -f "$session_pid" ]] && kill -0 "$(cat "$session_pid")" 2>/dev/null; then
    echo "Session helper running (PID $(cat "$session_pid"))."
  else
    launchctl print "gui/$uid/$label"
  fi
  exit
fi
if [[ "$action" != "install" && "$action" != "install-login" ]]; then
  echo "Usage: $0 install|install-login|status|uninstall" >&2
  exit 2
fi

mkdir -p "$support" "$logs" "$HOME/Library/LaunchAgents" "$userbin"
if ! command -v go >/dev/null 2>&1; then
  echo "Go is required to build h3ctl." >&2
  exit 1
fi
(cd "$root/cli" && go build -o "$support/h3ctl" ./cmd/h3ctl)

if [[ -x "$support/venv/bin/yt-dlp" ]]; then
  ytdlp="$support/venv/bin/yt-dlp"
elif command -v yt-dlp >/dev/null 2>&1; then
  ytdlp="$(command -v yt-dlp)"
else
  if ! command -v uv >/dev/null 2>&1; then
    echo "Install yt-dlp or uv before enabling the local helper." >&2
    exit 1
  fi
  uv venv "$support/venv"
  uv pip install --python "$support/venv/bin/python" yt-dlp
  ytdlp="$support/venv/bin/yt-dlp"
fi

for name in h3ctl yt-dlp; do
  target="$support/h3ctl"
  [[ "$name" == "yt-dlp" ]] && target="$ytdlp"
  if [[ ! -e "$userbin/$name" && ! -L "$userbin/$name" ]] || [[ -L "$userbin/$name" && "$(readlink "$userbin/$name")" == "$support/"* ]]; then
    ln -sfn "$target" "$userbin/$name"
  fi
done

origin="${H3_STUDIO_LOCAL_ORIGIN:-http://127.0.0.1:16020}"
launchctl bootout "gui/$uid" "$agent" 2>/dev/null || true
if [[ "$action" == "install-login" ]]; then
python3 - "$agent" "$label" "$support/h3ctl" "$ytdlp" "$origin" "$logs" <<'PY'
import plistlib
import sys

path, label, h3ctl, ytdlp, origin, logs = sys.argv[1:]
with open(path, "wb") as file:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": [h3ctl, "douyin", "serve", "--listen", "127.0.0.1:8765", "--studio-origin", origin,
                             "--cookies-from-browser", "chrome", "--yt-dlp", ytdlp],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(__import__("pathlib").Path(h3ctl).parent),
        "StandardOutPath": f"{logs}/douyin-helper.log",
        "StandardErrorPath": f"{logs}/douyin-helper-error.log",
    }, file)
PY
chmod 600 "$agent"
else
  rm -f "$agent"
fi
if [[ -f "$session_pid" ]]; then
  pid="$(cat "$session_pid")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && ps -p "$pid" -o command= | grep -Fq "$support/h3ctl douyin serve"; then
    kill "$pid"
  fi
  rm "$session_pid"
fi
if [[ "$action" == "install-login" ]]; then
  launchctl bootstrap "gui/$uid" "$agent"
  launchctl kickstart "gui/$uid/$label"
else
  python3 - "$support/h3ctl" "$ytdlp" "$origin" "$logs" "$session_pid" <<'PY'
import pathlib
import os
import subprocess
import sys

h3ctl, ytdlp, origin, logs, pid_path = sys.argv[1:]
with open(pathlib.Path(logs) / "douyin-helper.log", "ab") as stdout, \
     open(pathlib.Path(logs) / "douyin-helper-error.log", "ab") as stderr:
    process = subprocess.Popen(
        [h3ctl, "douyin", "serve", "--listen", "127.0.0.1:8765", "--studio-origin", origin,
         "--cookies-from-browser", "chrome", "--yt-dlp", ytdlp],
        stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
        cwd=str(pathlib.Path(h3ctl).parent), start_new_session=True,
        env={key: value for key, value in os.environ.items()
             if key in {"HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL"}},
    )
pathlib.Path(pid_path).write_text(f"{process.pid}\n")
PY
fi

ready=false
for _ in {1..50}; do
  if curl -fsS --max-time 1 -H "Origin: $origin" http://127.0.0.1:8765/health 2>/dev/null | grep -Fq '"api_version":"h3ctl.douyin/v1"'; then
    ready=true
    break
  fi
  sleep 0.1
done
if [[ "$ready" != true ]]; then
  echo "Douyin helper did not become ready; check $logs/douyin-helper-error.log." >&2
  exit 1
fi
if [[ "$action" == "install-login" ]]; then
  echo "Login helper installed for $origin. macOS must grant Full Disk Access to h3ctl and yt-dlp to read Chrome cookies after login."
else
  echo "Session helper installed for $origin. It reads Chrome cookies only when Studio requests an import."
fi
