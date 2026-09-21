"""Durable voice-conversion tasks backed by exclusive GPU workers."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ApiError
from .gpu_resources import GpuResourceManager
from .security import validate_id
from .storage import AssetStore, JsonStore


ENGINES = {"vevo2", "yingmusic"}


class ProcessVoiceWorker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._engine: str | None = None
        self._stderr = None
        # Capability reads must not wait for a model load or conversion that
        # holds the worker's run lock for minutes.
        self._status: tuple[subprocess.Popen[str] | None, str | None] = (None, None)

    def run(self, engine: str, request: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        with self._lock:
            process = self._ensure(engine)
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps({"action": "run", **request}, separators=(",", ":")) + "\n")
            process.stdin.flush()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                while True:
                    if cancel.is_set():
                        self.stop()
                        raise ApiError(409, "voice_canceled", "voice conversion was canceled")
                    if process.poll() is not None:
                        raise ApiError(502, "voice_worker_crashed", "voice worker exited unexpectedly")
                    if selector.select(timeout=0.25):
                        line = process.stdout.readline()
                        if not line:
                            raise ApiError(502, "voice_worker_crashed", "voice worker closed its response stream")
                        value = json.loads(line)
                        if not isinstance(value, dict) or value.get("ok") is not True:
                            message = str(value.get("error", "voice worker failed")) if isinstance(value, dict) else "voice worker returned invalid data"
                            raise ApiError(502, "voice_worker_failed", message)
                        return value
            finally:
                selector.close()

    def _ensure(self, engine: str) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None and self._engine == engine:
            return self._process
        self.stop()
        capability = voice_capability(self.config, engine)
        if not capability["available"]:
            raise ApiError(
                503, "voice_engine_unavailable", str(capability["reason"]),
                details={key: value for key, value in capability.items() if key not in {"root", "python"}},
            )
        root = Path(str(capability["root"]))
        python = str(capability["python"])
        log_root = self.config.data_root / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        self._stderr = (log_root / f"voice-worker-{engine}.log").open("a", encoding="utf-8")
        command = [
            python, "-u", str(Path(__file__).with_name("voice_worker.py")),
            "--engine", engine, "--repo", str(root),
            "--cache-root", str(self.config.data_root / "model-cache"),
            "--device", str(self.config.gpu_device_index),
        ]
        if engine == "vevo2":
            command.extend(["--model-revision", self.config.vevo2_model_revision])
        if engine == "yingmusic":
            command.extend([
                "--separator-config", self.config.yingmusic_separator_config,
                "--separator-checkpoint", self.config.yingmusic_separator_checkpoint,
                "--svc-config", self.config.yingmusic_svc_config,
                "--svc-checkpoint", self.config.yingmusic_svc_checkpoint,
            ])
        process = subprocess.Popen(
            command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._stderr, text=True, bufsize=1, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            events = selector.select(timeout=self.config.voice_worker_start_seconds)
            if not events:
                raise ApiError(504, "voice_worker_start_timeout", "voice worker model loading timed out")
            line = process.stdout.readline()
            ready = json.loads(line) if line else {}
            if process.poll() is not None or not isinstance(ready, dict) or ready.get("ready") is not True:
                raise ApiError(502, "voice_worker_start_failed", str(ready.get("error", "voice worker failed to start")))
        except Exception:
            self._terminate(process)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if self._stderr is not None:
                self._stderr.close()
                self._stderr = None
            raise
        finally:
            selector.close()
        self._process = process
        self._engine = engine
        self._status = (process, engine)
        return process

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        """Terminate the worker's complete process group, including SoX."""
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (AttributeError, OSError):
            process.terminate()
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (AttributeError, OSError):
            process.kill()
        process.wait(timeout=5)

    def stop(self) -> None:
        with self._lock:
            process, stderr = self._process, self._stderr
            self._process = None
            self._engine = None
            self._stderr = None
            self._status = (None, None)
            if process is not None:
                self._terminate(process)
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
            if stderr is not None:
                stderr.close()

    def status(self) -> dict[str, Any]:
        process, engine = self._status
        running = process is not None and process.poll() is None
        return {"running": running, "engine": engine if running else None}


class VoiceTaskManager:
    def __init__(
        self,
        config: Config,
        assets: AssetStore,
        resources: GpuResourceManager,
        worker: ProcessVoiceWorker | None = None,
    ) -> None:
        self.config = config
        self.assets = assets
        self.resources = resources
        self.worker = worker or ProcessVoiceWorker(config)
        self.store = JsonStore(config.data_root / "metadata" / "voice-tasks")
        self.output_root = config.data_root / "voice-results"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.resources.register_backend("voice", self.worker.stop)
        for task in self.store.list():
            if task.get("status") in {"queued", "running", "cancelling"}:
                try:
                    task_id = validate_id(str(task.get("id", "")), "voice task id")
                except ApiError:
                    # Never derive a recursive-delete target from malformed
                    # durable metadata.  A valid task id is exactly 32 hex.
                    continue
                shutil.rmtree(self.output_root / task_id, ignore_errors=True)
                task.update({
                    "status": "failed", "stage": "interrupted",
                    "error": {"code": "voice_task_interrupted", "message": "server restarted during voice conversion", "retryable": True},
                    "updated_at": time.time(),
                })
                self.store.put(task_id, task)

    def submit(self, data: dict[str, Any]) -> dict[str, Any]:
        allowed = {"engine", "source_asset_id", "reference_asset_id", "request_id"}
        if set(data) - allowed:
            raise ApiError(400, "invalid_parameter", "voice conversion contains unsupported fields")
        engine = str(data.get("engine", ""))
        if engine not in ENGINES:
            raise ApiError(400, "invalid_engine", "engine must be vevo2 or yingmusic")
        source_id = validate_id(str(data.get("source_asset_id", "")), "source asset id")
        reference_id = validate_id(str(data.get("reference_asset_id", "")), "reference asset id")
        source, reference = self.assets.get(source_id), self.assets.get(reference_id)
        if source.get("kind") != "audio" or reference.get("kind") != "audio":
            raise ApiError(400, "voice_media_kind", "source and reference assets must both be audio")
        capability = voice_capability(self.config, engine)
        if not capability["available"]:
            raise ApiError(
                503, "voice_engine_unavailable", str(capability["reason"]),
                details={key: value for key, value in capability.items() if key not in {"root", "python"}},
            )
        request_id = validate_id(str(data.get("request_id", uuid.uuid4().hex)), "request id")
        digest_value = {"engine": engine, "source_asset_id": source_id, "reference_asset_id": reference_id}
        digest = hashlib.sha256(json.dumps(digest_value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            duplicate = next((task for task in self.store.list() if task.get("request_id") == request_id), None)
            if duplicate:
                if duplicate.get("request_sha256") != digest:
                    raise ApiError(409, "idempotency_conflict", "request_id was already used with different voice inputs")
                return {**self.public(duplicate), "idempotent_replay": True}
            active = [task for task in self.store.list() if task.get("status") in {"queued", "running", "cancelling"}]
            if len(active) >= self.config.max_active_voice_tasks:
                raise ApiError(429, "voice_task_limit", f"at most {self.config.max_active_voice_tasks} active voice tasks are allowed")
            task_id = uuid.uuid4().hex
            now = time.time()
            task = {
                "id": task_id, "task_id": task_id, "request_id": request_id,
                "request_sha256": digest, "engine": engine,
                "source_asset_id": source_id, "reference_asset_id": reference_id,
                "status": "queued", "stage": "waiting_for_gpu", "progress": 0,
                "created_at": now, "updated_at": now,
            }
            self.store.put(task_id, task)

            def run(cancel: threading.Event, update) -> dict[str, Any]:
                return self._run(task_id, cancel, update)

            resource_id = self.resources.submit(task_id, "voice", voice_model_key(self.config, engine), run)
            task["resource_task_id"] = resource_id
            self.store.put(task_id, task)
        return self.public(task)

    def _run(self, task_id: str, cancel: threading.Event, update) -> dict[str, Any]:
        task = self.store.get(task_id)
        output_dir = self.output_root / task_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "converted.wav"
        self._update(task_id, status="running", stage="loading_model", progress=5)
        update("loading_model", 0.05)
        try:
            source = self.assets.content_path(self.assets.get(str(task["source_asset_id"])))
            reference = self.assets.content_path(self.assets.get(str(task["reference_asset_id"])))
            self._update(task_id, stage="inference", progress=15)
            update("inference", 0.15)
            result = self.worker.run(str(task["engine"]), {
                "task_id": task_id, "source": str(source), "reference": str(reference), "output": str(output),
            }, cancel)
            if cancel.is_set():
                raise ApiError(409, "voice_canceled", "voice conversion was canceled")
            if not output.is_file() or output.stat().st_size <= 0:
                raise ApiError(502, "voice_output_missing", "voice worker produced no output")
            if output.stat().st_size > self.config.max_audio_bytes:
                raise ApiError(507, "voice_output_too_large", "voice output exceeds the configured audio size limit")
            sha256 = self.assets.hash_file(output)
            completed = self._update(
                task_id, status="completed", stage="completed", progress=100,
                output={
                    "filename": "converted.wav", "mime_type": "audio/wav",
                    "size": output.stat().st_size, "sha256": sha256,
                    "download_url": f"/api/voice/tasks/{task_id}/download",
                },
            )
            update("completed", 1.0)
            return {"task_id": task_id, "output": completed["output"], "worker": result}
        except ApiError as error:
            shutil.rmtree(output_dir, ignore_errors=True)
            status = "canceled" if cancel.is_set() or error.code == "voice_canceled" else "failed"
            self._update(task_id, status=status, stage=status, error={
                "code": error.code, "message": error.message, "retryable": error.status >= 500,
            })
            raise
        except Exception as error:
            shutil.rmtree(output_dir, ignore_errors=True)
            self._update(task_id, status="failed", stage="failed", error={
                "code": "voice_internal_error", "message": str(error), "retryable": True,
            })
            raise

    def _update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            task = self.store.get(task_id)
            task.update(changes)
            task["updated_at"] = time.time()
            self.store.put(task_id, task)
            return task

    def get(self, task_id: str) -> dict[str, Any]:
        return self.public(self.store.get(validate_id(task_id, "voice task id")))

    def list(self) -> dict[str, Any]:
        return {"items": [self.public(task) for task in self.store.list()]}

    def cancel(self, task_id: str) -> dict[str, Any]:
        task_id = validate_id(task_id, "voice task id")
        with self._lock:
            task = self.store.get(task_id)
            if task.get("status") in {"completed", "failed", "canceled"}:
                return self.public(task)
            resource_id = str(task.get("resource_task_id", ""))
        if resource_id:
            try:
                self.resources.cancel(resource_id)
            except KeyError:
                pass
        with self._lock:
            current = self.store.get(task_id)
            if current.get("status") in {"completed", "failed", "canceled"}:
                return self.public(current)
            status = "canceled" if current.get("status") == "queued" else "cancelling"
            return self.public(self._update(task_id, status=status, stage=status))

    def output_path(self, task_id: str) -> Path:
        task = self.store.get(validate_id(task_id, "voice task id"))
        if task.get("status") != "completed" or not isinstance(task.get("output"), dict):
            raise ApiError(409, "voice_not_completed", "voice task has no completed output")
        path = self.output_root / task_id / "converted.wav"
        if not path.is_file():
            raise ApiError(404, "voice_output_missing", "voice output no longer exists")
        return path

    def delete(self, task_id: str) -> dict[str, Any]:
        task_id = validate_id(task_id, "voice task id")
        with self._lock:
            task = self.store.get(task_id)
            if task.get("status") not in {"completed", "failed", "canceled"}:
                raise ApiError(409, "voice_task_active", "cancel the active voice task before deleting it")
            shutil.rmtree(self.output_root / task_id, ignore_errors=True)
            self.store.delete(task_id)
        return {"id": task_id, "task_id": task_id, "deleted": True}

    def public(self, task: dict[str, Any]) -> dict[str, Any]:
        value = {key: task[key] for key in (
            "id", "task_id", "engine", "source_asset_id", "reference_asset_id",
            "status", "stage", "progress", "created_at", "updated_at", "output", "error",
        ) if key in task}
        resource_id = task.get("resource_task_id")
        if isinstance(resource_id, str) and task.get("status") == "queued":
            try:
                resource = self.resources.get(resource_id)
                value["queue_position"] = resource.get("queue_position")
                value["queue_reason"] = resource.get("queue_reason")
            except KeyError:
                pass
        task_id = str(task["id"])
        value.update({
            "status_url": f"/api/voice/tasks/{task_id}",
            "cancel_url": f"/api/voice/tasks/{task_id}/cancel",
        })
        return value

    def capabilities(self) -> dict[str, Any]:
        engines = []
        for engine in sorted(ENGINES):
            capability = voice_capability(self.config, engine)
            engines.append({key: value for key, value in capability.items() if key not in {"root", "python"}})
        return {"engines": engines, "worker": self.worker.status()}

    def stop(self) -> None:
        self.worker.stop()


def voice_model_key(config: Config, engine: str) -> str:
    if engine == "vevo2":
        return f"vevo2-fm:{config.vevo2_revision}:{config.vevo2_model_revision}"
    identities = [config.yingmusic_revision, config.yingmusic_model_revision]
    for value in (
        config.yingmusic_separator_config, config.yingmusic_separator_checkpoint,
        config.yingmusic_svc_config, config.yingmusic_svc_checkpoint,
    ):
        path = Path(value)
        try:
            stat = path.stat()
            identities.append(f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            identities.append(str(path))
    return "yingmusic-full:" + hashlib.sha256("\n".join(identities).encode()).hexdigest()


def voice_capability(config: Config, engine: str) -> dict[str, Any]:
    if engine == "vevo2":
        root, python = config.vevo2_root, config.vevo2_python
        required = [Path(root) / "models" / "svc" / "vevo2" / "infer_vevo2_fm.py"] if root else []
        mode = "fm_only"
    else:
        root, python = config.yingmusic_root, config.yingmusic_python
        required = [Path(value) for value in (
            config.yingmusic_separator_config, config.yingmusic_separator_checkpoint,
            config.yingmusic_svc_config, config.yingmusic_svc_checkpoint,
        ) if value]
        required.extend([Path(root) / "my_inference.py", Path(root) / "accom_separation" / "inference.py"] if root else [])
        mode = "separate_convert_remix"
    missing = []
    if not root or not Path(root).is_dir():
        missing.append("repository")
    if not python or not Path(python).is_file():
        missing.append("python")
    if engine == "yingmusic" and shutil.which("sox") is None:
        missing.append("sox")
    missing.extend(str(path.name) for path in required if not path.is_file())
    return {
        "id": engine, "available": not missing, "mode": mode,
        "root": root or None, "python": python or None,
        "repository_revision": config.vevo2_revision if engine == "vevo2" else config.yingmusic_revision,
        "model_revision": config.vevo2_model_revision if engine == "vevo2" else config.yingmusic_model_revision,
        "missing": missing,
        "reason": None if not missing else "missing configured voice runtime: " + ", ".join(missing),
    }
