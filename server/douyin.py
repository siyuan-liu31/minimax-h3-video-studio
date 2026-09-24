"""Durable Douyin parse/download tasks backed by a bounded yt-dlp process."""
from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import ApiError
from .security import validate_id
from .storage import JsonStore

HOSTS = {"douyin.com", "www.douyin.com", "m.douyin.com", "v.douyin.com", "iesdouyin.com", "www.iesdouyin.com"}
ACTIVE = {"queued", "running", "cancelling"}


def extract_url(text):
    if not isinstance(text, str) or not 1 <= len(text) <= 8192:
        raise ApiError(400, "invalid_link", "Provide a Douyin link or share text (maximum 8192 characters)")
    match = re.search(r'https://[^\s]+', text)
    try:
        url = urlsplit(match[0].rstrip(',.。，!?！？;；:)）]}》」\"\'') if match else "")
        if url.scheme != "https" or url.hostname not in HOSTS or url.username or url.password or url.port or not url.path:
            raise ValueError()
        return urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))
    except ValueError:
        raise ApiError(400, "invalid_link", "Only public Douyin HTTPS links are supported") from None


class DouyinTasks:
    def __init__(self, config, assets, mutation_lock, media):
        self.config, self.assets, self.mutation_lock, self.media = config, assets, mutation_lock, media
        self.store = JsonStore(config.data_root / "metadata" / "douyin-tasks")
        self.lock = threading.RLock()
        self.events, self.threads = {}, {}
        self.slots = threading.BoundedSemaphore(2)
        self.stopping = False
        self.executable = os.environ.get("H3_STUDIO_YTDLP", "yt-dlp")
        self.cookie_file = os.environ.get("H3_STUDIO_DOUYIN_COOKIES", "")
        self.timeout = 600
        for task in self.store.list():
            if task["status"] in ACTIVE:
                self.update(task["id"], status="failed", error={"code": "interrupted", "message": "Server restarted; retry this task", "retryable": True})

    def capabilities(self):
        available = all(shutil.which(x) for x in (self.executable, "ffmpeg", "ffprobe"))
        return {"available": available, "provider": "yt-dlp", "cookie_configured": bool(self.cookie_file),
                "reason": "" if available else "Install yt-dlp, ffmpeg and ffprobe on the Studio server", "max_video_bytes": self.config.max_video_bytes}

    def submit(self, request):
        if set(request) - {"text", "mode", "quality", "request_id"}:
            raise ApiError(400, "invalid_parameter", "Expected text, mode, quality and optional request_id")
        url = extract_url(request.get("text"))
        mode, quality = request.get("mode", "download"), request.get("quality", "best")
        if mode not in {"parse", "download"} or quality not in {"best", "1080", "720"}:
            raise ApiError(400, "invalid_parameter", "Invalid download mode or quality")
        request_id = validate_id(request.get("request_id", uuid.uuid4().hex), "request_id")
        with self.lock:
            for old in self.store.list():
                if old.get("request_id") == request_id:
                    if (old["url"], old["mode"], old["quality"]) != (url, mode, quality):
                        raise ApiError(409, "idempotency_conflict", "request_id already used with another request")
                    return self.public(old)
            if self.stopping or len(self.events) >= 8:
                raise ApiError(429, "download_queue_full", "Download queue is full; retry later")
            if not self.capabilities()["available"]:
                raise ApiError(503, "dependency_missing", self.capabilities()["reason"])
            now = time.time()
            task = {"id": uuid.uuid4().hex, "request_id": request_id, "url": url, "mode": mode, "quality": quality,
                    "status": "queued", "stage": "queued", "progress": 0, "created_at": now, "updated_at": now}
            self.store.put(task["id"], task)
            event = self.events[task["id"]] = threading.Event()
            worker = self.threads[task["id"]] = threading.Thread(target=self.run, args=(task, event), daemon=True)
            worker.start()
            return self.public(task)

    def update(self, task_id, **values):
        with self.lock:
            task = self.store.get(task_id)
            task.update(values, updated_at=time.time())
            self.store.put(task_id, task)
            return task

    def public(self, task):
        value = dict(task)
        value["task_id"] = task["id"]
        if task.get("asset_id"):
            try:
                value["asset"] = self.assets.public_metadata(self.assets.get(task["asset_id"]))
            except ApiError:
                value["asset_missing"] = True
        return value

    def get(self, task_id):
        return self.public(self.store.get(validate_id(task_id, "task_id")))

    def list(self):
        return {"tasks": [self.public(t) for t in self.store.list()[:100]]}

    def cancel(self, task_id):
        with self.lock:
            task = self.store.get(validate_id(task_id, "task_id"))
            if task["status"] in ACTIVE:
                # Asset import is the commit point; never report cancellation after importing.
                if task["stage"] == "importing":
                    raise ApiError(409, "import_in_progress", "Asset import is finishing; wait for completion")
                event = self.events.get(task_id)
                if event:
                    event.set()
                task = self.update(task_id, status="cancelling")
            return self.public(task)

    def retry(self, task_id):
        task = self.store.get(validate_id(task_id, "task_id"))
        if task["status"] not in {"failed", "canceled"}:
            raise ApiError(409, "task_not_retryable", "Only failed or canceled tasks can be retried")
        return self.submit({"text": task["url"], "mode": task["mode"], "quality": task["quality"]})

    def stop(self):
        with self.lock:
            self.stopping = True
            for event in self.events.values():
                event.set()
            workers = list(self.threads.values())
        for worker in workers:
            worker.join(timeout=2)

    def command(self, args, event, work, progress=None):
        base = [self.executable, "--ignore-config", "--no-plugin-dirs", "--no-playlist", "--no-colors",
                "--socket-timeout", "20", "--retries", "2", "--extractor-retries", "2"]
        if self.cookie_file:
            # yt-dlp may update its cookie jar; use a private disposable copy.
            cookie = work / "cookies.txt"
            if not cookie.exists():
                shutil.copyfile(self.cookie_file, cookie)
                cookie.chmod(0o600)
            base += ["--cookies", str(cookie)]
        process = subprocess.Popen(base + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        buffers = {"out": bytearray(), "err": bytearray()}
        pending = bytearray()
        deadline = time.monotonic() + self.timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "out")
                selector.register(process.stderr, selectors.EVENT_READ, "err")
                while selector.get_map():
                    if event.is_set():
                        raise ApiError(409, "canceled", "Download canceled")
                    if time.monotonic() > deadline:
                        raise ApiError(504, "download_timeout", "Download timed out; retry later")
                    if sum(p.stat().st_size for p in work.iterdir() if p.is_file()) > self.config.max_video_bytes * 2 + 8 * 1024 * 1024:
                        raise ApiError(413, "download_too_large", "Download exceeds the video size limit")
                    for key, _ in selector.select(0.2):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        buffers[key.data].extend(chunk)
                        if len(buffers[key.data]) > 8 * 1024 * 1024:
                            raise ApiError(502, "extractor_output_limit", "Extractor output exceeded its limit")
                        if key.data == "out" and progress:
                            pending.extend(chunk)
                            while b"\n" in pending:
                                line, _, rest = pending.partition(b"\n")
                                pending = bytearray(rest)
                                progress(line.decode("utf-8", "replace"))
            process.wait(timeout=5)
            if process.returncode:
                message = buffers["err"].decode("utf-8", "replace").lower()
                if "http error 429" in message or "too many requests" in message:
                    raise ApiError(429, "rate_limited", "Douyin is limiting requests. Stop repeated attempts and retry later.")
                if any(word in message for word in ("cookies", "sign in", "login", "captcha")):
                    raise ApiError(422, "cookie_refresh_required", "Douyin rejected the request. This does not prove that the session expired; verify the video in the browser and retry later.")
                if "http error 403" in message or "403 forbidden" in message:
                    raise ApiError(403, "access_restricted", "Douyin refused video access (HTTP 403). Verify the video in the browser and retry later.")
                raise ApiError(502, "extractor_failed", "Douyin extraction failed. Check the link, network and yt-dlp version, then retry.")
            return buffers["out"].decode("utf-8", "replace")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            process.stdout.close()
            process.stderr.close()

    def run(self, task, event):
        acquired = False
        task_id = task["id"]
        try:
            while not acquired:
                if event.is_set():
                    raise ApiError(409, "canceled", "Download canceled")
                acquired = self.slots.acquire(timeout=0.2)
            with tempfile.TemporaryDirectory(prefix="douyin-", dir=self.config.data_root / "tmp") as directory:
                work = Path(directory)
                self.update(task_id, status="running", stage="parsing")
                raw = json.loads(self.command(["--skip-download", "--dump-single-json", "--", task["url"]], event, work))
                if not raw.get("id") or raw.get("_type") == "playlist":
                    raise ApiError(422, "invalid_response", "Expected one Douyin video")
                metadata = {k: raw[k] for k in ("id", "title", "uploader", "duration", "width", "height") if k in raw}
                self.update(task_id, metadata=metadata)
                if task["mode"] == "parse":
                    with self.lock:
                        if event.is_set():
                            raise ApiError(409, "canceled", "Parsing canceled")
                        self.update(task_id, status="completed", stage="completed", progress=100)
                    return
                self.update(task_id, stage="downloading")
                last_progress = -1

                def progress(line):
                    nonlocal last_progress
                    if line.startswith("progress:"):
                        try:
                            p = json.loads(line[9:])
                            total = p.get("total_bytes") or p.get("total_bytes_estimate") or 0
                            percent = min(95, int(95 * p.get("downloaded_bytes", 0) / total)) if total else 0
                            if percent > last_progress:
                                last_progress = percent
                                self.update(task_id, progress=percent)
                        except (ValueError, TypeError, ZeroDivisionError):
                            pass
                fmt = "bv*+ba/b" if task["quality"] == "best" else f"bv*[height<={task['quality']}]+ba/b[height<={task['quality']}]"
                self.command(["--newline", "--progress", "--progress-template", "download:progress:%(progress)j", "--no-simulate",
                              "--max-filesize", str(self.config.max_video_bytes), "--merge-output-format", "mp4",
                              "-f", fmt, "-o", str(work / "video.%(ext)s"), "--", task["url"]], event, work, progress)
                files = [p for p in work.glob("video.*") if p.suffix in {".mp4", ".webm", ".mov", ".mkv"}]
                if len(files) != 1:
                    raise ApiError(422, "download_missing", "No complete video was downloaded (check the size limit)")
                with self.lock:
                    if event.is_set():
                        raise ApiError(409, "canceled", "Download canceled")
                    self.update(task_id, stage="importing", progress=97)
                source = files[0]
                digest = self.assets.hash_file(source)
                with self.mutation_lock:
                    asset = self.assets.find_library_duplicate(digest, requested_kind="video")
                    reused = asset is not None
                    if asset is None:
                        if self.assets.used_bytes() + self.media.quota_bytes() + source.stat().st_size > self.config.max_asset_storage_bytes:
                            raise ApiError(507, "asset_quota", "Asset storage quota exceeded")
                        asset = self.assets.import_file(source, original_filename=f"douyin-{raw['id']}{source.suffix}", requested_kind="video")
                        if self.assets.used_bytes() + self.media.quota_bytes() > self.config.max_asset_storage_bytes:
                            self.assets.delete(asset["id"])
                            raise ApiError(507, "asset_quota", "Normalized video exceeds storage quota")
                    self.update(task_id, status="completed", stage="completed", progress=100, asset_id=asset["id"], reused=reused)
        except ApiError as error:
            canceled = error.code == "canceled" or (event.is_set() and self.store.get(task_id).get("stage") != "importing")
            self.update(task_id, status="canceled" if canceled else "failed", error={"code": "canceled" if canceled else error.code, "message": "Download canceled" if canceled else error.message, "retryable": not canceled})
        except Exception:
            self.update(task_id, status="failed", error={"code": "download_failed", "message": "Download or asset import failed; check dependencies and retry", "retryable": True})
        finally:
            if acquired:
                self.slots.release()
            with self.lock:
                self.events.pop(task_id, None)
                self.threads.pop(task_id, None)
