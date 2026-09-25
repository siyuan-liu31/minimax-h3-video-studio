"""Durable voice-conversion tasks backed by exclusive GPU workers."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
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
from .voice_mix import remix_audio, mix_parameters


ENGINES = {"vevo2", "yingmusic", "soulx", "acestep"}
YINGMUSIC_DEFAULT_STEPS = 100
YINGMUSIC_DEFAULT_CFG = 0.7
YINGMUSIC_MAX_SEED = 2**32 - 1
YINGMUSIC_OUTPUT_DEFAULTS = {"include_stems": False, "echo": True, "reverb": True}
YINGMUSIC_TRACK_FILES = {"mix": "converted.wav", "dry_vocal": "dry-vocal.wav", "accompaniment": "accompaniment.wav"}


def yingmusic_parameters(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return canonical request parameters and effective worker parameters."""
    steps = data.get("diffusion_steps", YINGMUSIC_DEFAULT_STEPS)
    cfg = data.get("inference_cfg_rate", YINGMUSIC_DEFAULT_CFG)
    seed = data.get("seed", -1)
    if type(steps) is not int or not 10 <= steps <= 200:
        raise ApiError(400, "invalid_parameter", "diffusion_steps must be an integer from 10 to 200")
    if type(cfg) not in (int, float) or not 0 <= cfg <= 2:
        raise ApiError(400, "invalid_parameter", "inference_cfg_rate must be a number from 0 to 2")
    if type(seed) is not int or not -1 <= seed <= YINGMUSIC_MAX_SEED:
        raise ApiError(400, "invalid_parameter", "seed must be -1 or an integer from 0 to 4294967295")
    requested = {"diffusion_steps": steps, "inference_cfg_rate": float(cfg), "seed": seed}
    effective = {**requested, "seed": secrets.randbelow(YINGMUSIC_MAX_SEED + 1) if seed == -1 else seed}
    return requested, effective


def yingmusic_output_options(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict) or set(value) - set(YINGMUSIC_OUTPUT_DEFAULTS):
        raise ApiError(400, "invalid_parameter", "output_options must contain only include_stems, echo and reverb")
    options = {**YINGMUSIC_OUTPUT_DEFAULTS, **value}
    if any(type(option) is not bool for option in options.values()):
        raise ApiError(400, "invalid_parameter", "output_options values must be booleans")
    return options


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
        if engine == "acestep":
            command.extend(["--acestep-models", self.config.acestep_models])
        if engine == "soulx":
            command.extend(["--soulx-models", self.config.soulx_models])
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
        tuning = {"diffusion_steps", "inference_cfg_rate", "seed"}
        allowed = {"engine", "source_asset_id", "reference_asset_id", "request_id", "output_options", "lyrics", "original_lyrics", "operation", "preview", "caption", "audio_cover_strength"} | tuning
        if set(data) - allowed:
            raise ApiError(400, "invalid_parameter", "voice conversion contains unsupported fields")
        engine = str(data.get("engine", ""))
        if engine not in ENGINES:
            raise ApiError(400, "invalid_engine", "engine must be vevo2, yingmusic, soulx or acestep")
        if engine not in {"yingmusic", "soulx", "acestep"} and set(data) & tuning:
            raise ApiError(400, "invalid_parameter", "tuning is supported only for yingmusic or soulx")
        if engine != "yingmusic" and "output_options" in data:
            raise ApiError(400, "invalid_parameter", "output_options are supported only for yingmusic")
        if engine not in {"soulx", "acestep"} and {"lyrics", "original_lyrics"} & set(data):
            raise ApiError(400, "invalid_parameter", "lyrics require soulx or acestep")
        if engine != "acestep" and {"caption", "audio_cover_strength"} & set(data):
            raise ApiError(400, "invalid_parameter", "cover settings require acestep")
        if engine == "acestep" and (data.get("preview") is True or data.get("operation", "convert") != "convert"):
            raise ApiError(400, "invalid_parameter", "ACE-Step supports full cover generation only")
        operation = data.get("operation", "convert")
        preview = data.get("preview", False)
        if not isinstance(operation, str) or operation not in {"convert", "transcribe"} or type(preview) is not bool:
            raise ApiError(400, "invalid_parameter", "invalid voice operation or preview")
        if engine not in {"soulx", "acestep"} and ("operation" in data or "preview" in data):
            raise ApiError(400, "invalid_parameter", "transcription and preview require soulx")
        if operation == "transcribe" and (preview or set(data) & (tuning | {"lyrics", "original_lyrics", "output_options"})):
            raise ApiError(400, "invalid_parameter", "transcription accepts source audio only")
        lyrics = {}
        if operation == "transcribe":
            requested, parameters = {}, {}
        elif engine == "acestep":
            from .acestep import rewrite_parameters
            lyrics, requested, parameters = rewrite_parameters(data)
        elif engine == "soulx":
            from .soulx import rewrite_parameters
            lyrics, requested, parameters = rewrite_parameters(data)
        else:
            requested, parameters = yingmusic_parameters(data) if engine == "yingmusic" else ({}, {})
        output_options = yingmusic_output_options(data["output_options"]) if "output_options" in data else dict(YINGMUSIC_OUTPUT_DEFAULTS)
        source_id = validate_id(str(data.get("source_asset_id", "")), "source asset id")
        reference_id = validate_id(str(data.get("reference_asset_id", source_id if engine in {"soulx", "acestep"} else "")), "reference asset id")
        source, reference = self.assets.get(source_id), self.assets.get(reference_id)
        if engine == "acestep":
            duration = source.get("media", {}).get("duration")
            if type(duration) not in (int, float) or not 10 <= duration <= 180:
                raise ApiError(400, "invalid_duration", "ACE-Step 当前支持 10–180 秒歌曲，请先裁剪片段。")
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
        if engine == "yingmusic" and set(data) & tuning:
            digest_value["parameters"] = requested
        if "output_options" in data:
            digest_value["output_options"] = output_options
        if engine in {"soulx", "acestep"}:
            if operation == "transcribe":
                digest_value["operation"] = operation
            if preview:
                digest_value["preview"] = True
            digest_value.update(lyrics)
            digest_value["parameters"] = requested
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
            if engine == "yingmusic":
                task["parameters"] = parameters
                task["output_options"] = output_options
            if engine in {"soulx", "acestep"}:
                task.update(operation=operation, preview=preview)
                task.update(lyrics)
                if operation != "transcribe":
                    task["parameters"] = parameters
                    task["output_options"] = {"include_stems": engine == "soulx", "echo": False, "reverb": False}
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
        transcribe = task.get("operation") == "transcribe"
        output = output_dir / ("transcription.json" if transcribe else "converted.wav")
        self._update(task_id, status="running", stage="loading_model", progress=5)
        update("loading_model", 0.05)
        try:
            source = self.assets.content_path(self.assets.get(str(task["source_asset_id"])))
            reference = self.assets.content_path(self.assets.get(str(task["reference_asset_id"])))
            self._update(task_id, stage="transcribing" if transcribe else "inference", progress=15)
            update("transcribing" if transcribe else "inference", 0.15)
            worker_request = {
                "task_id": task_id, "source": str(source), "reference": str(reference), "output": str(output),
            }
            if task["engine"] == "yingmusic":
                worker_request["parameters"] = task["parameters"]
                worker_request["output_options"] = task.get("output_options", YINGMUSIC_OUTPUT_DEFAULTS)
            if task["engine"] in {"soulx", "acestep"}:
                worker_request.update({key: task[key] for key in ("lyrics", "original_lyrics", "parameters", "output_options", "operation", "preview") if key in task})
            result = self.worker.run(str(task["engine"]), worker_request, cancel)
            if cancel.is_set():
                raise ApiError(409, "voice_canceled", "voice conversion was canceled")
            if not output.is_file() or output.stat().st_size <= 0:
                raise ApiError(502, "voice_output_missing", "voice worker produced no output")
            if transcribe:
                if output.stat().st_size > 200000:
                    raise ApiError(502, "invalid_transcription", "transcription exceeds size limit")
                recognized = json.loads(output.read_text(encoding="utf-8")).get("lyrics")
                if not isinstance(recognized, str) or not recognized.strip() or len(recognized) > 10000:
                    raise ApiError(422, "empty_transcription", "未识别到有效歌词，请换一段清晰的演唱或手动填写原词。")
                completed = self._update(task_id, status="completed", stage="completed", progress=100, detected_lyrics=recognized)
                update("completed", 1.0)
                return {"task_id": task_id, "detected_lyrics": completed["detected_lyrics"]}
            tracks = {"mix": output}
            if task["engine"] in {"yingmusic", "soulx"} and task.get("output_options", {}).get("include_stems"):
                tracks.update({key: output_dir / name for key, name in YINGMUSIC_TRACK_FILES.items() if key != "mix"})
            for track_path in tracks.values():
                if not track_path.is_file() or track_path.stat().st_size <= 0:
                    raise ApiError(502, "voice_output_missing", "voice worker produced an incomplete audio track")
                if track_path.stat().st_size > self.config.max_audio_bytes:
                    raise ApiError(507, "voice_output_too_large", "voice output exceeds the configured audio size limit")
            outputs = {
                track: {
                    "filename": track_path.name, "mime_type": "audio/wav",
                    "size": track_path.stat().st_size, "sha256": self.assets.hash_file(track_path),
                    "preview_url": f"/api/voice/tasks/{task_id}/preview?track={track}",
                    "download_url": f"/api/voice/tasks/{task_id}/download?track={track}",
                } for track, track_path in tracks.items()
            }
            legacy_output = {**outputs["mix"], "download_url": f"/api/voice/tasks/{task_id}/download"}
            completed = self._update(
                task_id, status="completed", stage="completed", progress=100,
                output=legacy_output, outputs=outputs,
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

    def output_path(self, task_id: str, track: str = "mix") -> Path:
        task_id = validate_id(task_id, "voice task id")
        if track not in {*YINGMUSIC_TRACK_FILES, "remix"}:
            raise ApiError(400, "voice_track_invalid", "unknown voice output track")
        task = self.store.get(task_id)
        if task.get("status") != "completed" or not isinstance(task.get("output"), dict):
            raise ApiError(409, "voice_not_completed", "voice task has no completed output")
        if track != "mix" and (not isinstance(task.get("outputs"), dict) or track not in task["outputs"]):
            raise ApiError(404, "voice_track_missing", "voice output track was not retained")
        path = self.output_root / task_id / ("remix.wav" if track == "remix" else YINGMUSIC_TRACK_FILES[track])
        if not path.is_file():
            raise ApiError(404, "voice_output_missing", "voice output no longer exists")
        return path

    def remix(self, task_id: str, data: dict[str, Any]) -> dict[str, Any]:
        parameters = mix_parameters(data)
        task_id = validate_id(task_id, "voice task id")
        # Serialize replacement against deletion and concurrent remix requests.
        with self._lock:
            vocal = self.output_path(task_id, "dry_vocal")
            backing = self.output_path(task_id, "accompaniment")
            output = self.output_root / task_id / "remix.wav"
            remix_audio(vocal, backing, output, parameters)
            task = self.store.get(task_id)
            outputs = dict(task.get("outputs", {}))
            outputs["remix"] = {"filename": output.name, "size": output.stat().st_size,
                                "sha256": self.assets.hash_file(output)}
            return self.public(self._update(task_id, outputs=outputs, mix_parameters=parameters))

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
            "status", "stage", "progress", "created_at", "updated_at", "output", "outputs", "output_options", "error", "parameters", "lyrics", "original_lyrics", "detected_lyrics", "operation", "preview", "mix_parameters",
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
        for engine in ("vevo2", "yingmusic", "soulx", "acestep"):
            capability = voice_capability(self.config, engine)
            if engine == "yingmusic":
                capability["tuning"] = {
                    "diffusion_steps": {"default": YINGMUSIC_DEFAULT_STEPS, "minimum": 10, "maximum": 200},
                    "inference_cfg_rate": {"default": YINGMUSIC_DEFAULT_CFG, "minimum": 0, "maximum": 2},
                    "seed": {"default": -1, "minimum": -1, "maximum": YINGMUSIC_MAX_SEED},
                }
                capability["output_options"] = dict(YINGMUSIC_OUTPUT_DEFAULTS)
            if engine == "soulx":
                capability["lyrics"] = {"max_length": 10000, "languages": ["Mandarin"], "preserve_melody": True, "transcribe": True, "preview": True}
                capability["tuning"] = {
                    "diffusion_steps": {"default": 32, "minimum": 16, "maximum": 100},
                    "inference_cfg_rate": {"default": 3, "minimum": 0, "maximum": 10},
                    "seed": {"default": -1, "minimum": -1, "maximum": YINGMUSIC_MAX_SEED},
                }
            engines.append({key: value for key, value in capability.items() if key not in {"root", "python"}})
        return {"engines": engines, "worker": self.worker.status()}

    def stop(self) -> None:
        self.worker.stop()


def voice_model_key(config: Config, engine: str) -> str:
    if engine == "acestep":
        from .acestep import MODEL_REVISION, REPOSITORY_REVISION
        return "acestep-xl-sft:" + hashlib.sha256(f"{MODEL_REVISION}:{REPOSITORY_REVISION}:{config.acestep_models}".encode()).hexdigest()
    if engine == "soulx":
        identity = f"{config.soulx_revision}:{config.soulx_models}"
        return "soulx:" + hashlib.sha256(identity.encode()).hexdigest()
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
    if engine == "acestep":
        from .acestep import capability
        return capability(config)
    if engine == "soulx":
        from .soulx import capability
        return capability(config)
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
