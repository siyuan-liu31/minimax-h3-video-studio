"""Threaded HTTP API for the MiniMax H3 Video Studio web client."""

from __future__ import annotations

import hmac
import hashlib
import json
import mimetypes
import os
from email.utils import formatdate, parsedate_to_datetime
import shutil
import time
import urllib.parse
import uuid
import threading
import math
import re
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .comfy import ComfyClient, find_outputs
from .comfy_tasks import ComfyTaskCoordinator
from .checkpoints import CheckpointManager
from .character_migration import capability as character_migration_capability
from .character_migration import plan as plan_character_migration
from .config import Config
from .director_workflows import director_workflow_index, director_workflow_preset
from .errors import ApiError
from .h3_reference import estimate_packed_tokens, public_safety_policy, risk_assessment
from .gpu_resources import GpuResourceManager
from .multipart import MultipartPart, parse_multipart
from .profiles import DEFAULT_REGISTRY, ProfileRegistry
from .replication import capability as replication_capability
from .replication import plan as plan_replication
from .security import safe_filename, secure_join, validate_id
from .storage import AssetFolderStore, AssetStore, JobStore
from .media import MediaService
from .media_tasks import MediaTaskManager
from .scene_analysis import SceneAnalysisService
from .video_projects import VideoProjectManager
from .voice import VoiceTaskManager
from .workflows import ResumeSamplingPlan, compile_prompt_request, compile_workflow, parse_generation_request, workflow_evidence


def _disk_free(path: Path) -> int:
    """Return free bytes on the nearest existing filesystem ancestor."""
    candidate = path.resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free


@dataclass(slots=True)
class Runtime:
    config: Config
    assets: AssetStore
    jobs: JobStore
    comfy: ComfyClient
    registry: ProfileRegistry = field(default_factory=lambda: DEFAULT_REGISTRY)
    mutation_lock: threading.RLock = field(default_factory=threading.RLock)
    result_import_lock: threading.Lock = field(default_factory=threading.Lock)
    upload_slots: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(2))
    projects: VideoProjectManager = field(init=False)
    folders: AssetFolderStore = field(init=False)
    media: MediaService = field(init=False)
    media_tasks: MediaTaskManager = field(init=False)
    scene_analysis: SceneAnalysisService = field(init=False)
    checkpoints: CheckpointManager = field(init=False)
    resources: GpuResourceManager = field(init=False)
    voice: VoiceTaskManager = field(init=False)
    comfy_tasks: ComfyTaskCoordinator = field(init=False)
    instance_id: str = field(init=False)

    def __post_init__(self) -> None:
        if "*" in self.config.cors_origins and not self.config.api_key:
            raise ValueError("wildcard CORS requires a non-empty MiniMax H3 Video Studio API key")
        self.resources = GpuResourceManager(
            self.config.gpu_device_index,
            idle_release_seconds=self.config.gpu_idle_release_seconds,
        )
        comfy_release = getattr(self.comfy, "free_memory", None)
        if callable(comfy_release):
            self.resources.register_backend("comfy", comfy_release)
        self.comfy_tasks = ComfyTaskCoordinator(
            self.jobs, self.comfy, self.resources,
            poll_seconds=self.config.gpu_poll_seconds,
        )
        self.projects = VideoProjectManager(
            self.config, self.assets, self.jobs, self.comfy, self.registry, self.mutation_lock,
            comfy_tasks=self.comfy_tasks,
        )
        self.folders = AssetFolderStore(self.config.data_root / "metadata" / "asset-folders")
        self.media = MediaService(self.config, self.assets, self.mutation_lock)
        self.media_tasks = MediaTaskManager(self.config.data_root, self.media)
        self.scene_analysis = SceneAnalysisService(self.assets)
        self.checkpoints = CheckpointManager(
            self.config, self.jobs, self.assets, self.registry, self.mutation_lock,
        )
        self.voice = VoiceTaskManager(self.config, self.assets, self.resources)
        identity_path = self.config.data_root / "metadata" / "dataset-id"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            identity = identity_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            identity = ""
        if len(identity) != 32 or any(char not in "0123456789abcdef" for char in identity):
            identity = uuid.uuid4().hex
            temporary = identity_path.with_name(f"dataset-id.tmp-{uuid.uuid4().hex}")
            temporary.write_text(identity, encoding="ascii")
            os.replace(temporary, identity_path)
        self.instance_id = identity


class H3StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], runtime: Runtime):
        self.runtime = runtime
        super().__init__(address, handler)
        runtime.checkpoints.start_gc()

    def server_close(self) -> None:
        self.runtime.media_tasks.stop()
        self.runtime.checkpoints.stop_gc()
        self.runtime.voice.stop()
        self.runtime.resources.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "H3Studio/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def runtime(self) -> Runtime:
        return self.server.runtime  # type: ignore[attr-defined,no-any-return]

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {format_string % args}")

    def _cors_origin(self) -> str | None:
        origin = self.headers.get("Origin", "")
        allowed = self.runtime.config.cors_origins
        if "*" in allowed:
            return "*"
        if origin and origin in allowed:
            return origin
        return None

    def end_headers(self) -> None:
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            if origin != "*":
                self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, Authorization, Range, If-None-Match, If-Range, If-Modified-Since")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "Content-Disposition, Content-Length, Content-Range, Accept-Ranges, ETag, Last-Modified")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _json(self, status: int, value: Any, *, cache_control: str = "no-store", etag: bool = False) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        entity_tag = f'"{hashlib.sha256(body).hexdigest()}"' if etag else None
        if entity_tag and self._etag_matches(self.headers.get("If-None-Match", ""), entity_tag):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", entity_tag)
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if entity_tag:
            self.send_header("ETag", entity_tag)
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    @staticmethod
    def _etag_matches(raw: str, etag: str) -> bool:
        return any(value.strip() in {"*", etag} for value in raw.split(",") if value.strip())

    def _json_attachment(self, status: int, value: Any, filename: str) -> None:
        body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename(filename)}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _error(self, error: ApiError) -> None:
        self.close_connection = True
        self._json(error.status, error.as_dict())

    def _authorized(self) -> bool:
        expected = self.runtime.config.api_key
        if not expected:
            return True
        supplied = self.headers.get("X-API-Key", "")
        authorization = self.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        return hmac.compare_digest(expected, supplied)

    def _require_auth(self) -> None:
        if not self._authorized():
            raise ApiError(401, "unauthorized", "missing or invalid API key")

    def _require_origin(self) -> None:
        """Reject browser origins outside the explicit allowlist.

        A missing Origin is deliberately accepted for curl/CLI clients.  Merely
        omitting CORS response headers is insufficient because cross-origin
        writes would still execute before a browser blocks the response.
        """

        origin = self.headers.get("Origin")
        if origin is None:
            return
        allowed = self.runtime.config.cors_origins
        if "*" not in allowed and origin not in allowed:
            raise ApiError(403, "cors", "origin is not allowed")

    def _content_length(self) -> int:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "transfer_encoding", "chunked request bodies are not supported")
        raw = self.headers.get("Content-Length", "")
        try:
            length = int(raw)
        except ValueError as error:
            raise ApiError(400, "content_length", "valid Content-Length is required") from error
        if length < 0:
            raise ApiError(400, "content_length", "Content-Length cannot be negative")
        return length

    def _read_json(self, *, maximum: int | None = None) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(415, "content_type", "Content-Type must be application/json")
        length = self._content_length()
        maximum = maximum or self.runtime.config.max_json_bytes
        if length <= 0 or length > maximum:
            raise ApiError(413 if length > maximum else 400, "json_size", f"JSON body must be 1..{maximum} bytes")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "truncated_body", "request body ended early")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(400, "invalid_json", f"invalid JSON: {error}") from error
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json", "JSON body must be an object")
        return value

    def _upload(self) -> None:
        config = self.runtime.config
        length = self._content_length()
        if not self.runtime.upload_slots.acquire(blocking=False):
            raise ApiError(429, "upload_busy", "two uploads are already being processed; retry shortly")
        try:
            self._upload_locked(config, length)
        finally:
            self.runtime.upload_slots.release()

    def _upload_locked(self, config: Config, length: int) -> None:
        maximum = max(config.max_image_bytes, config.max_video_bytes, config.max_audio_bytes)
        parts = parse_multipart(
            self.rfile,
            content_type=self.headers.get("Content-Type", ""),
            content_length=length,
            temp_dir=config.data_root / "tmp",
            max_total_bytes=maximum + 1024 * 1024,
        )
        file_parts = [part for part in parts if part.name == "file" and part.temp_path]
        fields = {part.name: part.value for part in parts if part.value is not None}
        if len(file_parts) != 1:
            for part in parts:
                if part.temp_path:
                    part.temp_path.unlink(missing_ok=True)
            raise ApiError(400, "upload_file", "multipart request must contain exactly one file field")
        file_part: MultipartPart = file_parts[0]
        try:
            kind_raw = fields.get("kind", b"auto") or b"auto"
            try:
                kind = kind_raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ApiError(400, "upload_kind", "kind field must be UTF-8") from error
            kind = kind.strip().lower()
            content_sha256 = self.runtime.assets.hash_file(file_part.temp_path)
            reused = False
            with self.runtime.mutation_lock:
                duplicate = self.runtime.assets.find_library_duplicate(content_sha256, requested_kind=kind)
                if duplicate:
                    file_part.temp_path.unlink(missing_ok=True)
                    asset = duplicate
                    reused = True
                else:
                    if self.runtime.assets.used_bytes() + self.runtime.media.quota_bytes() + file_part.temp_path.stat().st_size > config.max_asset_storage_bytes:
                        raise ApiError(507, "asset_quota", "asset storage quota would be exceeded; delete unused assets first")
                    asset = self.runtime.assets.import_file(
                        file_part.temp_path,
                        original_filename=file_part.filename or "upload",
                        requested_kind=kind,
                        claimed_content_type=file_part.content_type,
                    )
                    if self.runtime.assets.used_bytes() + self.runtime.media.quota_bytes() > config.max_asset_storage_bytes:
                        self.runtime.assets.delete(str(asset["id"]))
                        raise ApiError(507, "asset_quota", "normalized media would exceed the asset storage quota")
            for part in parts:
                if part.temp_path and part.temp_path != file_part.temp_path:
                    part.temp_path.unlink(missing_ok=True)
            public = self.runtime.assets.public_metadata(asset)
            self._json(HTTPStatus.OK if reused else HTTPStatus.CREATED, {
                **asset, **public, "sha256": asset["sha256"],
                "asset": public, "asset_id": asset["id"], "reused": reused,
            })
        except Exception:
            for part in parts:
                if part.temp_path:
                    part.temp_path.unlink(missing_ok=True)
            raise

    def _generate(self) -> None:
        data = self._read_json()
        request_id = validate_id(str(data.get("request_id", uuid.uuid4().hex)), "request id")
        request_value = {key: value for key, value in data.items() if key != "request_id"}
        request_sha256 = hashlib.sha256(json.dumps(request_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        with self.runtime.mutation_lock:
            duplicate = next((item for item in self.runtime.jobs.list() if item.get("request_id") == request_id), None)
            if duplicate:
                if duplicate.get("request_sha256") != request_sha256:
                    raise ApiError(409, "idempotency_conflict", "request_id was already used with a different generation payload")
                self._json(HTTPStatus.ACCEPTED, {**duplicate, "idempotent_replay": True, "status_url": f"/api/status?id={duplicate['id']}"})
                return
            active = [item for item in self.runtime.jobs.list() if item.get("status") in {"submitting", "queued", "running"}]
            if len(active) >= self.runtime.config.max_active_jobs:
                raise ApiError(429, "job_limit", f"at most {self.runtime.config.max_active_jobs} active jobs are allowed")
            spec = parse_generation_request(data, self.runtime.assets.get, self.runtime.registry)
            spec, reference_preflight = self._prepare_risky_references(spec, request_id=request_id)
            profile = self.runtime.registry.get(spec.profile_id)
            resume_policy = self.runtime.checkpoints.profile_policy(profile)
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "job_id": job_id,
                "request_id": request_id,
                "request_sha256": request_sha256,
                "prompt_id": None,
                "client_id": f"h3-studio-{job_id}",
                "status": "submitting",
                "output_type": spec.output_type,
                "raw_prompt": data.get("prompt", ""),
                "prompt_parts": data.get("parts", data.get("prompt_parts", {})),
                "prompt": spec.prompt,
                "negative_prompt": spec.negative_prompt,
                "parameters": spec.public_parameters(),
                "director_mode": spec.director_mode or None,
                "source_asset_id": spec.source_asset_id or None,
                "graph": data.get("graph", {}),
                "references": [
                    {
                        "asset_id": reference.asset_id,
                        "kind": reference.kind,
                        "role": reference.role,
                        "tag_label": reference.label,
                        "include_audio": reference.include_audio,
                        "duration": reference.duration,
                        "voice_speaker": reference.voice_speaker,
                        "voice_subject": reference.voice_subject,
                        "content_hash": self.runtime.assets.get(reference.asset_id).get("sha256"),
                    }
                    for reference in spec.references
                ],
                "reference_preflight": reference_preflight,
                **({
                    "chain_id": job_id,
                    "parent_job_id": None,
                    "checkpoint_pending": True,
                } if resume_policy else {}),
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            job["submission_started_at"] = job["created_at"]
            self.runtime.jobs.put(job_id, job)
            try:
                self.runtime.comfy.ensure_capability(spec, self.runtime.config, self.runtime.registry)
                resume_plan = ResumeSamplingPlan(
                    mode="initial", max_total_steps=int(resume_policy["max_total_steps"]),
                ) if resume_policy else None
                workflow = compile_workflow(spec, self.runtime.config, job_id, resume_plan)
                workflow_json = json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                workflow_sha256 = hashlib.sha256(workflow_json).hexdigest()
                evidence_directory = self.runtime.config.data_root / "evidence" / "workflows"
                evidence_directory.mkdir(parents=True, exist_ok=True)
                evidence_path = evidence_directory / f"{job_id}.json"
                temporary_path = evidence_directory / f"{job_id}.json.tmp"
                temporary_path.write_bytes(workflow_json)
                temporary_path.replace(evidence_path)
                job["workflow_sha256"] = workflow_sha256
                job["workflow_evidence"] = {
                    **workflow_evidence(workflow, spec, job_id),
                    "sha256": workflow_sha256,
                    "reference_preflight": reference_preflight,
                }
                job["parameters"].update({
                    "diffusion_model": job["workflow_evidence"]["diffusion_model"],
                    "lora": job["workflow_evidence"]["lora"],
                })
                self.runtime.jobs.put(job_id, job)
                job = self.runtime.comfy_tasks.schedule(job_id, workflow)
            except ApiError as error:
                job.update({"status": "failed", "message": error.message, "error_code": error.code, "updated_at": time.time()})
                self.runtime.jobs.put(job_id, job)
                raise
            except Exception:
                job.update({"status": "failed", "message": "generation submission failed", "error_code": "internal_error", "updated_at": time.time()})
                self.runtime.jobs.put(job_id, job)
                raise
            if job.get("status") == "failed":
                raise ApiError(502, str(job.get("error_code", "comfy_rejected")), str(job.get("message", "generation submission failed")))
        self._json(
            HTTPStatus.ACCEPTED,
            {
                **job,
                "status_url": f"/api/status?id={job_id}",
            },
        )

    def _prepare_risky_references(self, spec, *, request_id: str):
        if spec.output_type != "video" or spec.mode != "ref2va":
            return spec, None
        reference_media: list[dict[str, Any]] = []
        for reference in spec.references:
            if reference.kind != "video":
                continue
            asset = self.runtime.assets.get(reference.asset_id)
            media = asset.get("media") if isinstance(asset.get("media"), dict) else {}
            frames = int(media.get("frame_count", 0) or 0)
            if frames <= 0:
                frames = max(1, int(round(float(media.get("duration", 0) or 0) * float(media.get("fps", 24) or 24))))
            reference_media.append({
                "asset_id": reference.asset_id,
                "width": int(media.get("width", 0) or 0),
                "height": int(media.get("height", 0) or 0),
                "frames": frames,
            })
        estimate = estimate_packed_tokens(
            reference_media, target_width=spec.width, target_height=spec.height,
            target_frames=spec.frames,
        )
        environment_getter = getattr(self.runtime.comfy, "execution_environment", None)
        environment = environment_getter(self.runtime.config) if callable(environment_getter) else {
            "gpu_architecture": self.runtime.config.gpu_architecture,
            "attention_backend": self.runtime.config.attention_backend,
        }
        assessment = risk_assessment(
            int(estimate["total_tokens"]),
            gpu_architecture=str(environment.get("gpu_architecture", "unknown")),
            attention_backend=str(environment.get("attention_backend", "unknown")),
            threshold=self.runtime.config.h3_token_risk_threshold,
        )
        preflight: dict[str, Any] = {
            "estimate": estimate, "risk": assessment, "optimized": False, "derivations": [],
        }
        if not assessment["requires_reference_optimization"]:
            return spec, preflight
        replacements: dict[str, Any] = {}
        for reference in spec.references:
            if reference.kind != "video":
                continue
            source = self.runtime.assets.get(reference.asset_id)
            source_path = self.runtime.assets.content_path(source)
            try:
                receipt = self.runtime.media.derive(source_path, {
                    **source,
                    "source_receipt": {"type": "asset", "asset_id": reference.asset_id},
                }, {
                    "operation": "prepare_h3_reference",
                    "preset": "h3-low-token",
                    "audio": "keep" if reference.include_audio else "remove",
                })
                derived_asset = self.runtime.media.save_as_asset(str(receipt["id"]), visibility="internal")
            except ApiError as error:
                existing = error.details if isinstance(error.details, dict) else {}
                raise ApiError(error.status, error.code, error.message, details={
                    **existing,
                    "stage": "reference_preprocessing",
                    "retryable": True,
                    "request_id": request_id,
                    "source_asset_id": reference.asset_id,
                    "materialized_locators": [
                        f"media:{item['derivation_id']}" for item in preflight["derivations"]
                    ],
                }) from error
            media = derived_asset.get("media") if isinstance(derived_asset.get("media"), dict) else {}
            replacements[reference.asset_id] = replace(
                reference,
                asset_id=str(derived_asset["id"]),
                comfy_path=str(self.runtime.assets.get(str(derived_asset["id"]))["comfy_path"]),
                duration=float(media.get("duration", reference.duration) or 0),
                fps=float(media.get("fps", 24) or 24),
                has_audio=media.get("has_audio") is True,
                include_audio=reference.include_audio and media.get("has_audio") is True,
            )
            preflight["derivations"].append({
                "source_asset_id": reference.asset_id,
                "derivation_id": receipt["id"],
                "derived_asset_id": derived_asset["id"],
                "original": receipt.get("preprocessing", {}).get("source"),
                "output": receipt.get("preprocessing", {}).get("output"),
                "reused": receipt.get("reused") is True,
            })
        references = tuple(replacements.get(item.asset_id, item) for item in spec.references)
        source_asset_id = spec.source_asset_id
        if source_asset_id in replacements:
            source_asset_id = replacements[source_asset_id].asset_id
        preflight["optimized"] = bool(replacements)
        post_media = []
        for item in references:
            if item.kind != "video":
                continue
            asset = self.runtime.assets.get(item.asset_id)
            media = asset.get("media") if isinstance(asset.get("media"), dict) else {}
            frames = int(media.get("frame_count", 0) or 0) or max(1, int(round(float(media.get("duration", 0) or 0) * 24)))
            post_media.append({"asset_id": item.asset_id, "width": media.get("width"), "height": media.get("height"), "frames": frames})
        preflight["post_optimization_estimate"] = estimate_packed_tokens(
            post_media, target_width=spec.width, target_height=spec.height, target_frames=spec.frames,
        )
        return replace(
            spec, references=references, source_asset_id=source_asset_id,
            reference_duration_total=sum(item.duration for item in references),
        ), preflight

    def _resume_job(self, requested_job_id: str) -> None:
        data = self._read_json()
        if set(data) - {"additional_steps", "request_id"}:
            raise ApiError(400, "invalid_parameter", "resume only accepts additional_steps and request_id")
        additional = data.get("additional_steps")
        if isinstance(additional, bool) or not isinstance(additional, int) or additional <= 0:
            raise ApiError(400, "invalid_additional_steps", "additional_steps must be a positive integer")
        raw_request_id = data.get("request_id", uuid.uuid4().hex)
        if not isinstance(raw_request_id, str) or not 8 <= len(raw_request_id) <= 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", raw_request_id):
            raise ApiError(400, "invalid_request_id", "request_id must be 8..128 URL-safe characters")
        request_sha256 = hashlib.sha256(
            json.dumps({"requested_job_id": requested_job_id, "additional_steps": additional}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.runtime.mutation_lock:
            duplicate = next((item for item in self.runtime.jobs.list() if item.get("resume_request_id") == raw_request_id), None)
            if duplicate:
                if duplicate.get("resume_request_sha256") != request_sha256:
                    raise ApiError(409, "idempotency_conflict", "request_id was already used with a different resume payload")
                self._json(HTTPStatus.ACCEPTED, {
                    "job_id": duplicate["id"], "parent_job_id": duplicate.get("parent_job_id"),
                    "steps_before": duplicate.get("steps_before"), "additional_steps": duplicate.get("additional_steps"),
                    "steps_after": (duplicate.get("parameters") or {}).get("steps"),
                    "status": duplicate.get("status"), "idempotent_replay": True,
                })
                return
            requested = self.runtime.jobs.get(validate_id(requested_job_id, "job id"))
            requested_parameters = requested.get("parameters") if isinstance(requested.get("parameters"), dict) else {}
            requested_profile = self.runtime.registry.get(str(requested_parameters.get("profile_id", "")))
            if not self.runtime.checkpoints.profile_policy(requested_profile):
                raise ApiError(409, "resume_unsupported", "the current Profile does not support resumable sampling")
            manifest = self.runtime.checkpoints.latest(requested)
            latest = self.runtime.jobs.get(validate_id(str(manifest["latest_job_id"]), "latest job id"))
            chain_id = validate_id(str(manifest["chain_id"]), "checkpoint chain id")
            busy = next((
                item for item in self.runtime.jobs.list()
                if str(item.get("chain_id") or item.get("id")) == chain_id
                and item.get("status") in {"submitting", "queued", "running"}
            ), None)
            if busy:
                raise ApiError(409, "resume_chain_busy", "another resume task is already running for this chain", details={"job_id": busy.get("id")})
            policy = self.runtime.checkpoints.profile_policy(
                self.runtime.registry.get(str((latest.get("parameters") or {}).get("profile_id", "")))
            )
            if not policy:
                raise ApiError(409, "resume_unsupported", "the current Profile does not support resumable sampling")
            lower, upper = policy.get("additional_steps", [1, int(policy["max_total_steps"])])
            if additional < int(lower) or additional > int(upper):
                raise ApiError(400, "invalid_additional_steps", f"additional_steps must be {lower}..{upper} for the current Profile")
            steps_before = int(manifest.get("steps", 0) or 0)
            steps_after = steps_before + additional
            if steps_after > int(policy["max_total_steps"]):
                raise ApiError(400, "max_steps_exceeded", "continued total steps exceed the current Profile maximum", details={
                    "steps_before": steps_before, "additional_steps": additional,
                    "steps_after": steps_after, "max_total_steps": policy["max_total_steps"],
                })
            active = [item for item in self.runtime.jobs.list() if item.get("status") in {"submitting", "queued", "running"}]
            if len(active) >= self.runtime.config.max_active_jobs:
                raise ApiError(429, "job_limit", f"at most {self.runtime.config.max_active_jobs} active jobs are allowed")
            spec = self.runtime.checkpoints.build_spec(latest, steps=steps_after)
            job_id = uuid.uuid4().hex
            checkpoint_locator, checkpoint_path = self.runtime.checkpoints.stage_input(manifest, job_id)
            job = {
                "id": job_id, "job_id": job_id,
                "request_id": raw_request_id,
                "resume_request_id": raw_request_id,
                "resume_request_sha256": request_sha256,
                "prompt_id": None, "client_id": f"h3-studio-{job_id}",
                "status": "submitting", "output_type": "video",
                "raw_prompt": latest.get("raw_prompt", ""),
                "prompt_parts": latest.get("prompt_parts", {}),
                "prompt": latest.get("prompt", ""), "negative_prompt": "",
                "parameters": spec.public_parameters(),
                "director_mode": latest.get("director_mode"),
                "source_asset_id": latest.get("source_asset_id"),
                "graph": latest.get("graph", {}),
                "references": latest.get("references", []),
                "chain_id": chain_id, "parent_job_id": latest["id"],
                "steps_before": steps_before, "additional_steps": additional,
                "checkpoint_pending": True,
                "checkpoint_input_staged": checkpoint_path.name,
                "created_at": time.time(), "updated_at": time.time(),
            }
            job["submission_started_at"] = job["created_at"]
            try:
                self.runtime.jobs.put(job_id, job)
                self.runtime.comfy.ensure_capability(spec, self.runtime.config, self.runtime.registry)
                workflow = compile_workflow(spec, self.runtime.config, job_id, ResumeSamplingPlan(
                    mode="resume", max_total_steps=int(policy["max_total_steps"]),
                    steps_before=steps_before, additional_steps=additional,
                    checkpoint_input=checkpoint_locator,
                ))
                workflow_json = json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                workflow_sha256 = hashlib.sha256(workflow_json).hexdigest()
                evidence_directory = self.runtime.config.data_root / "evidence" / "workflows"
                evidence_directory.mkdir(parents=True, exist_ok=True)
                temporary_path = evidence_directory / f"{job_id}.json.tmp"
                temporary_path.write_bytes(workflow_json)
                temporary_path.replace(evidence_directory / f"{job_id}.json")
                job["workflow_sha256"] = workflow_sha256
                job["workflow_evidence"] = {
                    **workflow_evidence(workflow, spec, job_id),
                    "sha256": workflow_sha256,
                    "chain_id": chain_id,
                    "parent_job_id": latest["id"],
                    "steps_before": steps_before,
                    "additional_steps": additional,
                    "steps_after": steps_after,
                    "checkpoint_format": manifest.get("format"),
                    "checkpoint_sha256": manifest.get("sha256"),
                }
                job["parameters"].update({
                    "diffusion_model": job["workflow_evidence"]["diffusion_model"],
                    "lora": job["workflow_evidence"]["lora"],
                })
                self.runtime.jobs.put(job_id, job)
                job = self.runtime.comfy_tasks.schedule(job_id, workflow)
            except ApiError as error:
                checkpoint_path.unlink(missing_ok=True)
                job.update({"status": "failed", "message": error.message, "error_code": error.code, "checkpoint_pending": False, "updated_at": time.time()})
                self.runtime.jobs.put(job_id, job)
                raise
            except Exception:
                checkpoint_path.unlink(missing_ok=True)
                job.update({"status": "failed", "message": "resume submission failed", "error_code": "internal_error", "checkpoint_pending": False, "updated_at": time.time()})
                self.runtime.jobs.put(job_id, job)
                raise
            if job.get("status") == "failed":
                raise ApiError(502, str(job.get("error_code", "comfy_rejected")), str(job.get("message", "resume submission failed")))
        self._json(HTTPStatus.ACCEPTED, {
            "job_id": job_id, "parent_job_id": latest["id"], "chain_id": chain_id,
            "steps_before": steps_before, "additional_steps": additional,
            "steps_after": steps_after, "status": job.get("status", "submitting"),
            "status_url": f"/api/status?id={job_id}",
        })

    def _with_resume(self, job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        resume = self.runtime.checkpoints.status(job)
        return {
            **result,
            "resume": resume,
            "can_resume": resume.get("can_resume", False),
            "resume_unavailable_reason": resume.get("reason"),
            "current_steps": resume.get("current_steps"),
            "max_total_steps": resume.get("max_total_steps"),
            "latest_job_id": resume.get("latest_job_id"),
            "checkpoint_created_at": resume.get("checkpoint_created_at"),
            "checkpoint_expires_at": resume.get("checkpoint_expires_at"),
        }

    def _job_status(self, job_id: str) -> dict[str, Any]:
        job = self.runtime.jobs.get(validate_id(job_id, "job id"))
        timestamps = {
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        }
        request_evidence = {
            "raw_prompt": job.get("raw_prompt", ""),
            "prompt_parts": job.get("prompt_parts", {}),
            "director_mode": job.get("director_mode"),
            "source_asset_id": job.get("source_asset_id"),
        }
        if job.get("status") == "completed" and isinstance(job.get("outputs"), list) and job["outputs"] and not job.get("checkpoint_pending"):
            return self._with_resume(job, {
                **timestamps, **request_evidence,
                "id": job_id, "job_id": job_id, "prompt_id": job.get("prompt_id"),
                "status": "completed", "state": "completed", "progress": 100,
                "output_type": job.get("output_type"), "parameters": job.get("parameters", {}),
                "prompt": job.get("prompt"), "references": job.get("references", []), "workflow_sha256": job.get("workflow_sha256"),
                "workflow_evidence": job.get("workflow_evidence"),
                "outputs": job["outputs"], "result_url": f"/api/result?id={job_id}",
                "preview_url": f"/api/preview?id={job_id}&index=0",
                "thumbnail_url": f"/api/jobs/{job_id}/thumbnail?index=0" if job.get("output_type") in {"image", "video"} else None,
                "download_url": f"/api/download?id={job_id}&index=0",
                "url": f"/api/download?id={job_id}&index=0",
            })
        if job.get("status") == "submitting" and not job.get("prompt_id"):
            resource_status = self.runtime.comfy_tasks.queued_status(job)
            if resource_status and resource_status.get("status") in {"queued", "running"}:
                return self._with_resume(job, {
                    **timestamps, **request_evidence, "id": job_id, "job_id": job_id,
                    "status": "submitting", "state": "submitting",
                    "progress": int(round(float(resource_status.get("progress", 0)) * 100)),
                    "message": resource_status.get("queue_reason") or resource_status.get("stage") or "waiting for GPU",
                    "queue_position": resource_status.get("queue_position"),
                    "queue_reason": resource_status.get("queue_reason"),
                    "gpu_stage": resource_status.get("stage"),
                    "output_type": job.get("output_type"), "parameters": job.get("parameters", {}),
                    "prompt": job.get("prompt"), "references": job.get("references", []),
                    "workflow_sha256": job.get("workflow_sha256"),
                    "workflow_evidence": job.get("workflow_evidence"),
                })
            try:
                recovered_prompt = self.runtime.comfy.find_prompt_by_client_id(str(job.get("client_id", "")))
            except ApiError:
                recovered_prompt = None
            if recovered_prompt:
                job.update({"prompt_id": recovered_prompt, "status": "queued", "updated_at": time.time()})
                self.runtime.jobs.put(job_id, job)
            elif time.time() - float(job.get("submission_started_at", job.get("created_at", 0)) or 0) >= self.runtime.config.submit_reconcile_grace_seconds:
                job.update({
                    "status": "failed",
                    "message": "ComfyUI submission could not be verified after the recovery grace period; it was not resubmitted",
                    "updated_at": time.time(),
                })
                self.runtime.jobs.put(job_id, job)
            else:
                return self._with_resume(job, {
                    **timestamps, **request_evidence, "id": job_id, "job_id": job_id,
                    "status": "submitting", "state": "submitting", "progress": 0,
                    "message": "reconciling submission with ComfyUI",
                    "output_type": job.get("output_type"), "parameters": job.get("parameters", {}),
                    "prompt": job.get("prompt"), "references": job.get("references", []),
                    "workflow_sha256": job.get("workflow_sha256"),
                    "workflow_evidence": job.get("workflow_evidence"),
                })
            timestamps["updated_at"] = job.get("updated_at")
        if job.get("status") in {"failed", "canceled"} or not job.get("prompt_id"):
            state = "canceled" if job.get("status") == "canceled" else "failed"
            self.runtime.checkpoints.cleanup_staged(job)
            return self._with_resume(job, {**timestamps, **request_evidence, "id": job_id, "job_id": job_id, "status": state, "state": state, "progress": 100, "message": str(job.get("message", "generation submission failed")), "output_type": job.get("output_type"), "parameters": job.get("parameters", {}), "prompt": job.get("prompt"), "references": job.get("references", []), "workflow_sha256": job.get("workflow_sha256"), "workflow_evidence": job.get("workflow_evidence")})
        comfy_status = self.runtime.comfy.status(str(job["prompt_id"]))
        state = comfy_status["status"]
        if state == "not_found":
            state = "failed"
            comfy_status["message"] = "ComfyUI no longer knows this prompt; it may have been cleared or the service restarted"
        elif state == "error":
            state = "failed"
        if state in {"completed", "failed", "canceled"}:
            self.runtime.comfy_tasks.notify_terminal(job, state)
        progress = {"queued": 0, "running": 50, "completed": 100, "failed": 100}.get(state, 0)
        result: dict[str, Any] = {
            **timestamps, **request_evidence,
            "id": job_id,
            "job_id": job_id,
            "prompt_id": job["prompt_id"],
            "status": state,
            "state": state,
            "progress": progress,
            "output_type": job["output_type"],
            "parameters": job["parameters"],
            "prompt": job.get("prompt"),
            "references": job.get("references", []),
            "workflow_sha256": job.get("workflow_sha256"),
            "workflow_evidence": job.get("workflow_evidence"),
        }
        record = comfy_status.get("record")
        if isinstance(record, dict):
            outputs = find_outputs(record, str(job["output_type"]))
            if outputs:
                enriched: list[dict[str, Any]] = []
                for output in outputs:
                    evidence = dict(output)
                    evidence["thumbnail_url"] = f"/api/jobs/{job_id}/thumbnail?index={len(enriched)}" if job.get("output_type") in {"image", "video"} else None
                    try:
                        path = secure_join(self.runtime.config.comfy_output, str(output.get("subfolder", "")), str(output.get("filename", "")))
                        digest = hashlib.sha256()
                        with path.open("rb") as source:
                            while chunk := source.read(1024 * 1024):
                                digest.update(chunk)
                        evidence.update({"size": path.stat().st_size, "sha256": digest.hexdigest(), "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"})
                        try:
                            media = AssetStore._probe_image(path) if job.get("output_type") == "image" else AssetStore._probe_media(path, "video")
                            evidence["media"] = media
                        except ApiError as error:
                            evidence["media_probe_error"] = error.code
                    except (OSError, ApiError):
                        evidence["evidence_error"] = "output file unavailable during evidence capture"
                    enriched.append(evidence)
                result["outputs"] = enriched
                result["result_url"] = f"/api/result?id={job_id}"
                result["preview_url"] = f"/api/preview?id={job_id}&index=0"
                result["thumbnail_url"] = f"/api/jobs/{job_id}/thumbnail?index=0" if job.get("output_type") in {"image", "video"} else None
                result["download_url"] = f"/api/download?id={job_id}&index=0"
                result["url"] = result["download_url"]
        if "message" in comfy_status:
            result["message"] = comfy_status["message"]
            result["error"] = comfy_status["message"]
        if "details" in comfy_status:
            result["details"] = comfy_status["details"]
        if state == "completed" and not result.get("outputs"):
            state = "failed"
            result.update({"status": "failed", "state": "failed", "message": "ComfyUI completed without an expected media output", "error": "ComfyUI completed without an expected media output"})
        if state != job.get("status") or state in {"completed", "failed"}:
            job.update({"status": state, "updated_at": time.time()})
            if isinstance(result.get("outputs"), list):
                job["outputs"] = result["outputs"]
            if "message" in result:
                job["message"] = result["message"]
            self.runtime.jobs.put(job_id, job)
            result["updated_at"] = job["updated_at"]
        if state == "completed" and job.get("checkpoint_pending") and isinstance(record, dict):
            try:
                checkpoint = self.runtime.checkpoints.capture(job, record)
                if checkpoint:
                    job.update({
                        "checkpoint_pending": False,
                        "checkpoint_id": checkpoint.get("checkpoint_id"),
                        "checkpoint_created_at": checkpoint.get("created_at"),
                        "checkpoint_expires_at": checkpoint.get("expires_at"),
                    })
            except ApiError as error:
                job.update({"checkpoint_pending": False, "checkpoint_error": error.code})
                result["checkpoint_error"] = error.as_dict()["error"]
            job["updated_at"] = time.time()
            self.runtime.jobs.put(job_id, job)
            result["updated_at"] = job["updated_at"]
        if state in {"completed", "failed"}:
            self.runtime.checkpoints.cleanup_staged(job)
        return self._with_resume(job, result)

    def _cancel_job(self, job_id: str) -> None:
        job_id = validate_id(job_id, "job id")
        with self.runtime.mutation_lock:
            job = self.runtime.jobs.get(job_id)
            if job.get("status") in {"completed", "failed", "canceled"}:
                self._json(HTTPStatus.OK, {"id": job_id, "status": job.get("status"), "already_terminal": True})
                return
            self.runtime.comfy_tasks.cancel(job)
            job.update({"status": "canceled", "message": "canceled by user", "updated_at": time.time()})
            self.runtime.jobs.put(job_id, job)
            self.runtime.checkpoints.cleanup_staged(job)
        self._json(HTTPStatus.OK, {"id": job_id, "status": "canceled"})

    def _asset_references(self, *, active_only: bool = False) -> set[str]:
        referenced = {
            str(reference.get("asset_id"))
            for job in self.runtime.jobs.list()
            if not active_only or str(job.get("status")) not in {"completed", "failed", "canceled"}
            for reference in (job.get("references", []) if isinstance(job.get("references"), list) else [])
            if isinstance(reference, dict) and reference.get("asset_id")
        }
        for project in self.runtime.projects.store.list():
            if active_only and str(project.get("status")) not in {"running", "stopping", "merging"}:
                continue
            storyboard = project.get("storyboard")
            if isinstance(storyboard, dict) and storyboard.get("source_asset_id"):
                referenced.add(str(storyboard["source_asset_id"]))
            recipe = project.get("recipe")
            if isinstance(recipe, dict):
                if recipe.get("source_asset_id"):
                    referenced.add(str(recipe["source_asset_id"]))
                for reference in recipe.get("references", []) if isinstance(recipe.get("references"), list) else []:
                    if isinstance(reference, dict) and reference.get("asset_id"):
                        referenced.add(str(reference["asset_id"]))
                for target in recipe.get("targets", []) if isinstance(recipe.get("targets"), list) else []:
                    if isinstance(target, dict) and target.get("character_asset_id"):
                        referenced.add(str(target["character_asset_id"]))
            for segment in project.get("segments", []):
                source_range = segment.get("source_range") if isinstance(segment, dict) else None
                if isinstance(source_range, dict) and source_range.get("asset_id"):
                    referenced.add(str(source_range["asset_id"]))
                request = segment.get("request", {}) if isinstance(segment, dict) else {}
                for reference in request.get("references", []) if isinstance(request, dict) else []:
                    if isinstance(reference, dict) and reference.get("asset_id"):
                        referenced.add(str(reference["asset_id"]))
                for attempt in segment.get("attempts", []) if isinstance(segment, dict) else []:
                    continuation = attempt.get("continuation", {}) if isinstance(attempt, dict) else {}
                    if isinstance(continuation, dict) and continuation.get("asset_id"):
                        referenced.add(str(continuation["asset_id"]))
                    derived_source = continuation.get("source_range") if isinstance(continuation, dict) else None
                    if isinstance(derived_source, dict) and derived_source.get("asset_id"):
                        referenced.add(str(derived_source["asset_id"]))
        voice_store = getattr(self.runtime.voice, "store", None)
        for task in voice_store.list() if voice_store is not None else []:
            if active_only and str(task.get("status")) not in {"queued", "running", "cancelling"}:
                continue
            for key in ("source_asset_id", "reference_asset_id"):
                if task.get(key):
                    referenced.add(str(task[key]))
        return referenced

    def _delete_asset(self, asset_id: str) -> None:
        asset_id = validate_id(asset_id, "asset id")
        with self.runtime.mutation_lock:
            if asset_id in self._asset_references():
                raise ApiError(409, "asset_in_use", "asset is referenced by a saved job or video project and cannot be deleted")
            asset = self.runtime.assets.delete(asset_id)
        self._json(HTTPStatus.OK, {"id": asset_id, "deleted": True, "filename": asset.get("filename")})

    def _project_references_job(self, project: Any, job_id: str) -> bool:
        if isinstance(project, dict):
            for key, value in project.items():
                if key in {"job_id", "result_job_id", "source_job_id"} and value == job_id:
                    return True
                if self._project_references_job(value, job_id):
                    return True
        elif isinstance(project, list):
            return any(self._project_references_job(value, job_id) for value in project)
        return False

    @staticmethod
    def _job_output_identity(output: Any) -> tuple[str, str] | None:
        if not isinstance(output, dict) or output.get("type", "output") != "output":
            return None
        filename = output.get("filename")
        subfolder = output.get("subfolder", "")
        if not isinstance(filename, str) or not isinstance(subfolder, str) or not filename:
            return None
        return subfolder, filename

    def _delete_job(self, job_id: str) -> None:
        job_id = validate_id(job_id, "job id")
        with self.runtime.mutation_lock:
            job = self.runtime.jobs.get(job_id)
            if str(job.get("status")) in {"submitting", "queued", "running"}:
                raise ApiError(409, "job_busy", "cancel the generation before deleting it")
            if any(self._project_references_job(project, job_id) for project in self.runtime.projects.store.list()):
                raise ApiError(409, "job_in_use", "result is referenced by a saved video project and cannot be deleted")
            other_outputs = {
                identity
                for other in self.runtime.jobs.list()
                if other.get("id") != job_id
                for output in (other.get("outputs", []) if isinstance(other.get("outputs"), list) else [])
                if (identity := self._job_output_identity(output)) is not None
            }
            output_paths: list[Path] = []
            for output in job.get("outputs", []) if isinstance(job.get("outputs"), list) else []:
                identity = self._job_output_identity(output)
                if identity is None or identity in other_outputs:
                    continue
                subfolder, filename = identity
                output_paths.append(secure_join(self.runtime.config.comfy_output, subfolder, filename))
            saved_asset_ids = [
                str(output.get("asset_id"))
                for output in (job.get("outputs", []) if isinstance(job.get("outputs"), list) else [])
                if isinstance(output, dict) and output.get("asset_id")
            ]
            self.runtime.jobs.delete(job_id)
            evidence = secure_join(self.runtime.config.data_root / "evidence" / "workflows", f"{job_id}.json")
            evidence.unlink(missing_ok=True)
            deleted_outputs = 0
            for path in output_paths:
                if path.is_file():
                    path.unlink()
                    deleted_outputs += 1
        self._json(HTTPStatus.OK, {
            "id": job_id,
            "deleted": True,
            "outputs_deleted": deleted_outputs,
            "saved_asset_ids_preserved": saved_asset_ids,
        })

    def _garbage_collect(self) -> None:
        data = self._read_json()
        dry_run = data.get("dry_run", True) is not False
        try:
            days = min(3650, max(1, int(data.get("older_than_days", self.runtime.config.asset_ttl_days))))
        except (TypeError, ValueError) as error:
            raise ApiError(400, "invalid_parameter", "older_than_days must be an integer") from error
        cutoff = time.time() - days * 86400
        def collect_candidates() -> list[dict[str, Any]]:
            referenced = self._asset_references()
            active_referenced = self._asset_references(active_only=True)
            return [
                asset for asset in self.runtime.assets.list()
                if asset.get("id") not in (
                    active_referenced
                    if asset.get("visibility") == "internal"
                    else referenced
                )
                and float(asset.get("created_at", 0)) < cutoff
            ]
        if dry_run:
            candidates = collect_candidates()
        else:
            # Candidate discovery and deletion share the mutation lock. A job
            # or video project created concurrently cannot gain a reference in
            # the gap after discovery but before destructive deletion.
            with self.runtime.mutation_lock:
                candidates = collect_candidates()
                for asset in candidates:
                    self.runtime.assets.delete(str(asset["id"]))
        temporary_media = {"derivation_receipts": 0, "derivation_files": 0, "thumbnails": 0}
        if not dry_run:
            temporary_media = self.runtime.media.garbage_collect(older_than_seconds=days * 86400)
        checkpoint_gc = {"manifests": 0, "files": 0, "temporary_files": 0}
        if not dry_run:
            checkpoint_gc = self.runtime.checkpoints.garbage_collect()
        self._json(HTTPStatus.OK, {"dry_run": dry_run, "older_than_days": days, "count": len(candidates), "bytes": sum(int(asset.get("storage_size", asset.get("size", 0))) for asset in candidates), "asset_ids": [asset.get("id") for asset in candidates], "temporary_media": temporary_media, "checkpoints": checkpoint_gc})

    def _result(self, job_id: str) -> None:
        status = self._job_status(job_id)
        if status["status"] != "completed" or not status.get("outputs"):
            raise ApiError(409, "result_not_ready", "generation result is not ready", details=status)
        self._json(HTTPStatus.OK, status)

    @staticmethod
    def _job_cursor(job: dict[str, Any]) -> tuple[float, str]:
        created = float(job.get("created_at", 0) or 0)
        if not math.isfinite(created):
            created = 0.0
        return created, str(job.get("id", ""))

    @staticmethod
    def _format_cursor(job: dict[str, Any]) -> str:
        created, job_id = Handler._job_cursor(job)
        return f"{created!r}:{job_id}"

    @staticmethod
    def _parse_cursor(raw: str) -> tuple[float, str]:
        if ":" not in raw:
            raise ValueError("cursor requires created_at and id separated by :")
        raw_created, job_id = raw.rsplit(":", 1)
        created_at = float(raw_created)
        if not math.isfinite(created_at):
            raise ValueError("cursor timestamp must be finite")
        return created_at, job_id

    @staticmethod
    def _visual_output_type(job: dict[str, Any]) -> str | None:
        declared = job.get("output_type")
        if declared in {"image", "video"}:
            return str(declared)
        outputs = job.get("outputs")
        if not isinstance(outputs, list) or not outputs or not isinstance(outputs[0], dict):
            return None
        filename = str(outputs[0].get("filename", "")).lower()
        content_type = mimetypes.guess_type(filename)[0] or ""
        if content_type.startswith("image/"):
            return "image"
        if content_type.startswith("video/"):
            return "video"
        return None

    def _listed_payload(self, job: dict[str, Any], *, summary: bool = False) -> dict[str, Any]:
        payload = {key: value for key, value in job.items() if key not in {"workflow", "graph"}}
        resume = self.runtime.checkpoints.status(job)
        payload.update({
            "resume": resume,
            "can_resume": resume.get("can_resume", False),
            "current_steps": resume.get("current_steps"),
            "max_total_steps": resume.get("max_total_steps"),
            "latest_job_id": resume.get("latest_job_id"),
            "checkpoint_expires_at": resume.get("checkpoint_expires_at"),
        })
        inferred_output_type = Handler._visual_output_type(job)
        if inferred_output_type and payload.get("output_type") not in {"image", "video"}:
            payload["output_type"] = inferred_output_type
        if summary:
            allowed = {
                "id", "job_id", "status", "progress", "message", "output_type",
                "parameters", "workflow_sha256", "created_at", "updated_at", "outputs", "prompt",
                "pinned",
                "resume", "can_resume", "current_steps", "max_total_steps",
                "latest_job_id", "checkpoint_expires_at",
            }
            payload = {key: value for key, value in payload.items() if key in allowed}
            if isinstance(payload.get("prompt"), str):
                payload["prompt"] = payload["prompt"][:512]
        job_id = str(job.get("id", ""))
        outputs = payload.get("outputs")
        if payload.get("status") == "completed" and isinstance(outputs, list) and outputs:
            # Old durable job receipts predate public media links.  Hydrate the
            # complete contract at read time so Results keeps working after a
            # release switch or machine clone.  Copy nested records rather
            # than mutating JsonStore values while serving a GET request.
            copied_outputs: list[Any] = []
            visual = inferred_output_type in {"image", "video"}
            for index, output in enumerate(outputs):
                if not isinstance(output, dict):
                    copied_outputs.append(output)
                    continue
                public_output = dict(output)
                public_output.update({
                    "preview_url": f"/api/preview?id={job_id}&index={index}",
                    "download_url": f"/api/download?id={job_id}&index={index}",
                })
                if visual:
                    public_output["thumbnail_url"] = f"/api/jobs/{job_id}/thumbnail?index={index}"
                copied_outputs.append(public_output)
            payload["outputs"] = copied_outputs
            payload.update({
                "result_url": f"/api/result?id={job_id}",
                "preview_url": f"/api/preview?id={job_id}&index=0",
                "download_url": f"/api/download?id={job_id}&index=0",
                "url": f"/api/download?id={job_id}&index=0",
            })
            if visual:
                payload["thumbnail_url"] = f"/api/jobs/{job_id}/thumbnail?index=0"
        return payload

    def _listed_jobs(self, *, limit: int, cursor: str | None, results_only: bool = False, summary: bool = False) -> tuple[list[dict[str, Any]], str | None]:
        cursor_value: tuple[float, str] | None = None
        if cursor is not None:
            try:
                cursor_value = self._parse_cursor(cursor)
            except (ValueError, TypeError, OverflowError) as error:
                raise ApiError(400, "invalid_pagination", "cursor must be \"created_at:id\"") from error
        stored_jobs = sorted(self.runtime.jobs.list(), key=lambda job: self._job_cursor(job), reverse=True)
        listed: list[dict[str, Any]] = []
        last_included: dict[str, Any] | None = None
        has_more = False
        for stored in stored_jobs:
            if results_only and not (
                stored.get("status") == "completed"
                and self._visual_output_type(stored) in {"image", "video"}
                and isinstance(stored.get("outputs"), list)
                and stored.get("outputs")
            ):
                continue
            if cursor_value is not None:
                created_at, job_id = self._job_cursor(stored)
                cursor_created, cursor_job_id = cursor_value
                if created_at > cursor_created or (created_at == cursor_created and job_id >= cursor_job_id):
                    continue
            if len(listed) >= limit:
                has_more = True
                break
            listed.append(self._listed_payload(stored, summary=summary))
            last_included = stored
        next_cursor = self._format_cursor(last_included) if has_more and last_included is not None else None
        return listed, next_cursor

    def _pinned_results(self, *, summary: bool = False) -> list[dict[str, Any]]:
        return [
            self._listed_payload(job, summary=summary)
            for job in self.runtime.jobs.list()
            if job.get("pinned") is True
            and job.get("status") == "completed"
            and self._visual_output_type(job) in {"image", "video"}
            and isinstance(job.get("outputs"), list)
            and job.get("outputs")
        ][:100]

    def _import_job_output(self, job_id: str) -> None:
        data = self._read_json()
        if set(data) - {"index", "display_name", "folder_id", "visibility"}:
            raise ApiError(400, "invalid_parameter", "only index, display_name, folder_id and visibility may be supplied")
        visibility = data.get("visibility", "library")
        if visibility not in {"library", "internal"}:
            raise ApiError(400, "invalid_visibility", "visibility must be library or internal")
        if visibility == "internal" and ({"display_name", "folder_id"} & set(data)):
            raise ApiError(400, "invalid_parameter", "internal assets cannot have library display_name or folder_id metadata")
        raw_index = data.get("index", 0)
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise ApiError(400, "output_index", "index must be an integer")
        folder_id = data.get("folder_id")
        if folder_id is not None:
            folder_id = validate_id(folder_id, "folder id")
            self.runtime.folders.get(folder_id)
        display_marker: Any = data["display_name"] if "display_name" in data else ...
        output_path, output = self._output_path(job_id, raw_index)
        job_id = validate_id(job_id, "job id")
        temporary: Path | None = None
        created_asset_id: str | None = None
        try:
            # A dedicated lock preserves per-result idempotency without blocking
            # generation submissions and unrelated metadata mutations while file
            # copy, probing or 24fps normalization is running.
            with self.runtime.result_import_lock:
                with self.runtime.mutation_lock:
                    job = self.runtime.jobs.get(job_id)
                    outputs = job.get("outputs", [])
                    if not isinstance(outputs, list) or not 0 <= raw_index < len(outputs):
                        raise ApiError(409, "result_not_ready", "generation result is not ready")
                    stored_output = outputs[raw_index]
                    if not isinstance(stored_output, dict):
                        raise ApiError(500, "job_metadata_corrupt", "stored output metadata is invalid")
                    existing_id = stored_output.get("asset_id")
                    existing = None
                    if isinstance(existing_id, str):
                        try:
                            existing = self.runtime.assets.get(existing_id)
                        except ApiError as error:
                            if error.status != 404:
                                raise
                    if existing is not None:
                        promoted = visibility == "library" and existing.get("visibility", "library") == "internal"
                        if visibility == "library" and (
                            promoted
                            or display_marker is not ...
                            or "folder_id" in data
                        ):
                            existing = self.runtime.assets.update_library_metadata(
                                existing_id, display_name=display_marker,
                                folder_id=folder_id if "folder_id" in data else ...,
                                folder_exists=self.runtime.folders.get,
                            )
                        if promoted:
                            stored_output["asset_visibility"] = "library"
                            job["outputs"] = outputs
                            self.runtime.jobs.put(job_id, job)
                        public = self.runtime.assets.public_metadata(existing)
                        response = {**public, "asset": public, "asset_id": public["id"], "reused": True}
                    else:
                        response = None
                        size = output_path.stat().st_size
                        if self.runtime.assets.used_bytes() + self.runtime.media.quota_bytes() + size > self.runtime.config.max_asset_storage_bytes:
                            raise ApiError(507, "asset_quota", "asset storage quota would be exceeded; delete unused assets first")
                        requested_kind = str(job.get("output_type", "auto"))
                if response is not None:
                    self._json(HTTPStatus.OK, response)
                    return

                temp_root = self.runtime.config.data_root / "tmp"
                temp_root.mkdir(parents=True, exist_ok=True)
                temporary = temp_root / f"result-{job_id}-{uuid.uuid4().hex}{output_path.suffix}"
                shutil.copy2(output_path, temporary)
                asset = self.runtime.assets.import_file(
                    temporary,
                    original_filename=str(output.get("filename") or output_path.name),
                    requested_kind=requested_kind,
                    claimed_content_type=str(output.get("mime_type") or mimetypes.guess_type(output_path.name)[0] or "application/octet-stream"),
                    visibility=visibility,
                )
                temporary = None
                created_asset_id = str(asset["id"])

                try:
                    with self.runtime.mutation_lock:
                        if self.runtime.assets.used_bytes() + self.runtime.media.quota_bytes() > self.runtime.config.max_asset_storage_bytes:
                            raise ApiError(507, "asset_quota", "normalized media would exceed the asset storage quota")
                        job = self.runtime.jobs.get(job_id)
                        outputs = job.get("outputs", [])
                        if not isinstance(outputs, list) or not 0 <= raw_index < len(outputs) or not isinstance(outputs[raw_index], dict):
                            raise ApiError(409, "result_not_ready", "generation result is not ready")
                        stored_output = outputs[raw_index]
                        if display_marker is not ... or "folder_id" in data:
                            asset = self.runtime.assets.update_library_metadata(
                                created_asset_id, display_name=display_marker,
                                folder_id=folder_id if "folder_id" in data else ...,
                                folder_exists=self.runtime.folders.get,
                            )
                        stored_output["asset_id"] = created_asset_id
                        stored_output["asset_visibility"] = visibility
                        stored_output["asset_imported_at"] = time.time()
                        job["outputs"] = outputs
                        self.runtime.jobs.put(job_id, job)
                        public = self.runtime.assets.public_metadata(asset)
                except Exception:
                    self.runtime.assets.delete(created_asset_id)
                    created_asset_id = None
                    raise
                self._json(HTTPStatus.CREATED, {**public, "asset": public, "asset_id": public["id"], "reused": False})
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _output_path(self, job_id: str, index: int) -> tuple[Path, dict[str, Any]]:
        status = self._job_status(job_id)
        outputs = status.get("outputs", [])
        if status["status"] != "completed" or not isinstance(outputs, list):
            raise ApiError(409, "result_not_ready", "generation result is not ready")
        if not 0 <= index < len(outputs):
            raise ApiError(404, "output_not_found", "output index does not exist")
        output = outputs[index]
        if output.get("type", "output") != "output":
            raise ApiError(403, "unsafe_output", "only permanent ComfyUI outputs can be downloaded")
        path = secure_join(
            self.runtime.config.comfy_output,
            str(output.get("subfolder", "")),
            str(output.get("filename", "")),
        )
        if not path.is_file():
            raise ApiError(404, "output_file_missing", "output file is missing")
        return path, output

    def _send_file(
        self,
        path: Path,
        *,
        download: bool,
        original_name: str | None = None,
        cache_control: str = "private, max-age=31536000, immutable",
    ) -> None:
        name = safe_filename(original_name or path.name)
        stat = path.stat()
        size = stat.st_size
        etag = f'"{stat.st_mtime_ns:x}-{size:x}"'
        last_modified = formatdate(stat.st_mtime, usegmt=True)
        if self._etag_matches(self.headers.get("If-None-Match", ""), etag) and not self.headers.get("Range"):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            return
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        if_range = self.headers.get("If-Range", "").strip()
        if range_header and if_range:
            matches = if_range == etag
            if not matches:
                try:
                    matches = parsedate_to_datetime(if_range).timestamp() >= int(stat.st_mtime)
                except (TypeError, ValueError, OverflowError):
                    matches = False
            if not matches:
                range_header = ""
        partial = False
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                self._send_range_error(size)
                return
            raw_start, separator, raw_end = range_header[6:].partition("-")
            try:
                if not separator:
                    raise ValueError
                if raw_start:
                    start = int(raw_start)
                    end = int(raw_end) if raw_end else size - 1
                else:
                    suffix = int(raw_end)
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, size - suffix)
                    end = size - 1
            except ValueError as error:
                self._send_range_error(size)
                return
            if start < 0 or end < start or start >= size:
                self._send_range_error(size)
                return
            end = min(end, size - 1)
            partial = True
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        disposition = "attachment" if download else "inline"
        self.send_header("Content-Disposition", f'{disposition}; filename="{name}"')
        self.send_header("Cache-Control", cache_control)
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_range_error(self, size: int) -> None:
        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_compiled_workflow(self, job_id: str, *, download: bool) -> None:
        job_id = validate_id(job_id, "job id")
        job = self.runtime.jobs.get(job_id)
        expected = job.get("workflow_sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            raise ApiError(409, "workflow_unavailable", "job has no durable compiled workflow")
        evidence_root = self.runtime.config.data_root / "evidence" / "workflows"
        path = secure_join(evidence_root, f"{job_id}.json")
        if not path.is_file():
            raise ApiError(404, "workflow_file_missing", "compiled workflow file is missing")
        encoded = path.read_bytes()
        digest = hashlib.sha256(encoded).hexdigest()
        if not hmac.compare_digest(expected, digest):
            raise ApiError(409, "workflow_integrity", "compiled workflow no longer matches its job receipt")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        disposition = "attachment" if download else "inline"
        self.send_header(
            "Content-Disposition",
            f'{disposition}; filename="{safe_filename(f"{job_id}-workflow.json")}"',
        )
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(encoded)

    def _job_thumbnail(self, job_id: str, index: int) -> Path:
        output_path, output = self._output_path(job_id, index)
        job = self.runtime.jobs.get(validate_id(job_id, "job id"))
        kind = self._visual_output_type(job) or ""
        if kind not in {"image", "video"}:
            raise ApiError(400, "thumbnail_unsupported", "this result has no visual thumbnail")
        cache_key = f"job:{job_id}:{index}:{output.get('sha256', '')}:{output_path.stat().st_mtime_ns}:{output_path.stat().st_size}"
        return self.runtime.media.thumbnail(output_path, cache_key=cache_key, kind=kind)

    def _asset_thumbnail(self, asset_id: str) -> Path:
        asset = self.runtime.assets.get(validate_id(asset_id, "asset id"))
        path = self.runtime.assets.content_path(asset)
        cache_key = f"asset:{asset_id}:{asset.get('sha256', '')}:{path.stat().st_mtime_ns}:{path.stat().st_size}"
        return self.runtime.media.thumbnail(path, cache_key=cache_key, kind=str(asset.get("kind", "")))

    def _derive_media(self) -> None:
        data = self._read_json()
        common = {"source", "operation", "display_name"}
        operation_fields = {
            "video_trim": {"start", "end"}, "audio_trim": {"start", "end"},
            "frame": {"position", "time"}, "extract_audio": set(), "remove_audio": set(),
            "prepare_h3_reference": {
                "preset", "max_short_edge", "max_long_edge", "fps", "max_duration",
                "audio", "fit", "alignment", "pad_mode", "background",
            },
        }
        operation = data.get("operation")
        if operation not in operation_fields or set(data) - common - operation_fields[operation]:
            raise ApiError(400, "invalid_operation", "operation or operation parameters are invalid")
        source = data.get("source")
        if not isinstance(source, dict) or set(source) - {"type", "asset_id", "job_id", "receipt_id", "index"}:
            raise ApiError(400, "invalid_source", "source must identify exactly one asset, job output, or derivation")
        source_type = source.get("type")
        if source_type == "asset":
            if set(source) != {"type", "asset_id"}:
                raise ApiError(400, "invalid_source", "asset source requires only type and asset_id")
            asset_id = validate_id(source.get("asset_id"), "asset id")
            asset = self.runtime.assets.get(asset_id)
            path = self.runtime.assets.content_path(asset)
            meta = {**asset, "source_receipt": {"type": "asset", "asset_id": asset_id}}
        elif source_type == "job":
            if set(source) - {"type", "job_id", "index"} or "job_id" not in source:
                raise ApiError(400, "invalid_source", "job source requires job_id and optional index")
            index = source.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int):
                raise ApiError(400, "output_index", "index must be an integer")
            job_id = validate_id(source.get("job_id"), "job id")
            path, output = self._output_path(job_id, index)
            job = self.runtime.jobs.get(job_id)
            meta = {
                "kind": job.get("output_type"), "media": output.get("media", {}),
                "source_receipt": {"type": "job", "job_id": job_id, "index": index},
            }
        elif source_type == "derivation":
            if set(source) != {"type", "receipt_id"}:
                raise ApiError(400, "invalid_source", "derivation source requires only type and receipt_id")
            receipt_id = validate_id(source.get("receipt_id"), "receipt id")
            receipt = self.runtime.media.get(receipt_id)
            path = self.runtime.media.path(receipt)
            meta = {
                **receipt,
                "source_receipt": {"type": "derivation", "receipt_id": receipt_id},
            }
        else:
            raise ApiError(400, "invalid_source", "source.type must be asset, job, or derivation")
        current_media = meta.get("media") if isinstance(meta.get("media"), dict) else {}
        if str(meta.get("kind")) in {"video", "audio"} and not float(current_media.get("duration", 0) or 0):
            meta["media"] = AssetStore._probe_media(path, str(meta["kind"]))
        background = data.pop("background", False)
        if not isinstance(background, bool):
            raise ApiError(400, "invalid_parameter", "background must be a boolean")
        if background:
            if operation != "prepare_h3_reference":
                raise ApiError(400, "invalid_parameter", "background processing is only supported for prepare_h3_reference")
            task = self.runtime.media_tasks.submit(path, meta, data)
            self._json(HTTPStatus.ACCEPTED, task)
            return
        receipt = self.runtime.media.derive(path, meta, data)
        self._json(HTTPStatus.CREATED, {**receipt, "receipt": receipt})

    def _resolve_media_source(self, source: Any) -> tuple[Path, dict[str, Any]]:
        """Resolve the same strict asset/job/derivation shape used by derive."""
        if not isinstance(source, dict) or set(source) - {"type", "asset_id", "job_id", "receipt_id", "index"}:
            raise ApiError(400, "invalid_source", "media source must identify exactly one asset, job output, or derivation")
        source_type = source.get("type")
        if source_type == "asset":
            if set(source) != {"type", "asset_id"}:
                raise ApiError(400, "invalid_source", "asset source requires only type and asset_id")
            asset_id = validate_id(source.get("asset_id"), "asset id")
            asset = self.runtime.assets.get(asset_id)
            path = self.runtime.assets.content_path(asset)
            meta = {**asset, "source_receipt": {"type": "asset", "asset_id": asset_id}}
        elif source_type == "job":
            if set(source) - {"type", "job_id", "index"} or "job_id" not in source:
                raise ApiError(400, "invalid_source", "job source requires job_id and optional index")
            index = source.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int):
                raise ApiError(400, "output_index", "index must be an integer")
            job_id = validate_id(source.get("job_id"), "job id")
            path, output = self._output_path(job_id, index)
            job = self.runtime.jobs.get(job_id)
            meta = {
                "kind": job.get("output_type"), "media": output.get("media", {}),
                "source_receipt": {"type": "job", "job_id": job_id, "index": index},
            }
        elif source_type == "derivation":
            if set(source) != {"type", "receipt_id"}:
                raise ApiError(400, "invalid_source", "derivation source requires only type and receipt_id")
            receipt_id = validate_id(source.get("receipt_id"), "receipt id")
            receipt = self.runtime.media.get(receipt_id)
            path = self.runtime.media.path(receipt)
            meta = {**receipt, "source_receipt": {"type": "derivation", "receipt_id": receipt_id}}
        else:
            raise ApiError(400, "invalid_source", "source.type must be asset, job, or derivation")
        current = meta.get("media") if isinstance(meta.get("media"), dict) else {}
        if str(meta.get("kind")) in {"video", "audio"} and not float(current.get("duration", 0) or 0):
            meta["media"] = AssetStore._probe_media(path, str(meta["kind"]))
        return path, meta

    def _save_derivation(self, receipt_id: str) -> None:
        data = self._read_json()
        if set(data) - {"display_name", "folder_id", "visibility"}:
            raise ApiError(400, "invalid_parameter", "only display_name, folder_id and visibility may be supplied")
        visibility = data.get("visibility", "library")
        if visibility not in {"library", "internal"}:
            raise ApiError(400, "invalid_visibility", "visibility must be library or internal")
        if visibility == "internal" and ({"display_name", "folder_id"} & set(data)):
            raise ApiError(400, "invalid_parameter", "internal assets cannot have library display_name or folder_id metadata")
        folder_marker: Any = data["folder_id"] if "folder_id" in data else ...
        if folder_marker is not ... and folder_marker is not None:
            folder_marker = validate_id(folder_marker, "folder id")
        display_marker: Any = data["display_name"] if "display_name" in data else ...
        with self.runtime.result_import_lock:
            public = self.runtime.media.save_as_asset(
                receipt_id, display_name=display_marker, folder_id=folder_marker,
                folder_exists=self.runtime.folders.get, visibility=visibility,
            )
        self._json(HTTPStatus.CREATED, {**public, "asset": public, "asset_id": public["id"]})

    def do_OPTIONS(self) -> None:
        try:
            self._require_origin()
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except ApiError as error:
            self._error(error)

    def do_POST(self) -> None:
        try:
            self._require_origin()
            self._require_auth()
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/assets":
                self._upload()
                return
            if path == "/api/asset-folders":
                data = self._read_json()
                if set(data) - {"name", "parent_id"}:
                    raise ApiError(400, "invalid_parameter", "only name and parent_id may be supplied")
                with self.runtime.mutation_lock:
                    folder = self.runtime.folders.create(data.get("name"), data.get("parent_id"))
                self._json(HTTPStatus.CREATED, {**folder, "folder": folder})
                return
            if path == "/api/media/derive":
                self._derive_media()
                return
            if path == "/api/media/mux-audio":
                data = self._read_json()
                if set(data) - {"video", "audio", "duration", "display_name"} or not {"video", "audio"} <= set(data):
                    raise ApiError(400, "invalid_operation", "mux-audio requires only video, audio, optional duration, and display_name")
                video_path, video_meta = self._resolve_media_source(data["video"])
                audio_path, audio_meta = self._resolve_media_source(data["audio"])
                receipt = self.runtime.media.mux_audio(video_path, video_meta, audio_path, audio_meta, data)
                self._json(HTTPStatus.CREATED, {**receipt, "receipt": receipt})
                return
            if path == "/api/media/analyze-scenes":
                self._json(HTTPStatus.OK, self.runtime.scene_analysis.analyze(self._read_json()))
                return
            if path == "/api/voice/tasks":
                self._json(HTTPStatus.ACCEPTED, self.runtime.voice.submit(self._read_json()))
                return
            segments = [segment for segment in path.split("/") if segment]
            if len(segments) == 4 and segments[:2] == ["api", "media-tasks"] and segments[3] == "cancel":
                if self.headers.get("Content-Length", "0") != "0":
                    self._read_json()
                self._json(HTTPStatus.ACCEPTED, self.runtime.media_tasks.cancel(segments[2]))
                return
            if len(segments) == 5 and segments[:3] == ["api", "voice", "tasks"] and segments[4] == "cancel":
                if self.headers.get("Content-Length", "0") != "0":
                    self._read_json()
                self._json(HTTPStatus.ACCEPTED, self.runtime.voice.cancel(segments[3]))
                return
            if path in {"/api/generate", "/generate"}:
                self._generate()
                return
            if path == "/api/prompts/compile":
                self._json(HTTPStatus.OK, compile_prompt_request(self._read_json(), self.runtime.assets.get))
                return
            if path == "/api/video/character-migration/plan":
                data = self._read_json(maximum=self.runtime.config.max_project_json_bytes)
                source_id = validate_id(data.get("source_asset_id"), "source_asset_id")
                targets = data.get("targets")
                target = targets[0] if isinstance(targets, list) and len(targets) == 1 and isinstance(targets[0], dict) else {}
                character_id = validate_id(target.get("character_asset_id"), "targets[0].character_asset_id")
                capabilities = self.runtime.comfy.capabilities(self.runtime.config, self.runtime.registry)
                available_profiles = {
                    str(item.get("id"))
                    for item in capabilities.get("profiles", [])
                    if isinstance(item, dict) and item.get("available") is True
                }
                motion = capabilities.get("video", {}).get("motion_context", {})
                used_context = sum(
                    int(item.get("size", 0) or 0)
                    for item in self.runtime.projects.motion_contexts.metadata.list()
                )
                result = plan_character_migration(
                    data,
                    source=self.runtime.assets.get(source_id),
                    character=self.runtime.assets.get(character_id),
                    registry=self.runtime.registry,
                    available_profiles=available_profiles,
                    motion_context_available=isinstance(motion, dict) and motion.get("available") is True,
                    free_disk_bytes=min(
                        _disk_free(self.runtime.config.data_root),
                        _disk_free(self.runtime.config.comfy_output),
                    ),
                    merged_quota_bytes=self.runtime.config.max_merged_output_bytes,
                    motion_context_quota_bytes=max(0, self.runtime.config.max_motion_context_storage_bytes - used_context),
                )
                self._json(HTTPStatus.OK, result)
                return
            if path == "/api/video/replication/plan":
                data = self._read_json(maximum=self.runtime.config.max_project_json_bytes)
                source_id = validate_id(data.get("source_asset_id"), "source_asset_id")
                raw_references = data.get("references", [])
                if not isinstance(raw_references, list):
                    raise ApiError(400, "invalid_replication", "references must be an array")
                reference_assets = [
                    self.runtime.assets.get(validate_id(item.get("asset_id"), "reference asset_id"))
                    for item in raw_references
                    if isinstance(item, dict)
                ]
                if len(reference_assets) != len(raw_references):
                    raise ApiError(400, "invalid_replication", "each reference must be an object")
                capabilities = self.runtime.comfy.capabilities(self.runtime.config, self.runtime.registry)
                available_profiles = {
                    str(item.get("id"))
                    for item in capabilities.get("profiles", [])
                    if isinstance(item, dict) and item.get("available") is True
                }
                motion = capabilities.get("video", {}).get("motion_context", {})
                result = plan_replication(
                    data,
                    source=self.runtime.assets.get(source_id),
                    reference_assets=reference_assets,
                    registry=self.runtime.registry,
                    available_profiles=available_profiles,
                    motion_context_available=isinstance(motion, dict) and motion.get("available") is True,
                )
                self._json(HTTPStatus.OK, result)
                return
            if segments == ["api", "video-projects"]:
                self._json(HTTPStatus.CREATED, self.runtime.projects.create(
                    self._read_json(maximum=self.runtime.config.max_project_json_bytes)
                ))
                return
            if len(segments) == 4 and segments[:2] == ["api", "video-projects"] and segments[3] in {"run", "stop", "merge"}:
                body = self._read_json()
                action_name = segments[3]
                if action_name == "run":
                    if set(body) - {"segment_ids"}:
                        raise ApiError(400, "invalid_action", "run only accepts segment_ids")
                    result = self.runtime.projects.run(segments[2], body.get("segment_ids"))
                else:
                    if body:
                        raise ApiError(400, "invalid_action", "project action body must be an empty JSON object")
                    result = getattr(self.runtime.projects, action_name)(segments[2])
                self._json(HTTPStatus.ACCEPTED, result)
                return
            if len(segments) == 6 and segments[:2] == ["api", "video-projects"] and segments[3] == "segments" and segments[5] == "run":
                if self._read_json():
                    raise ApiError(400, "invalid_action", "segment run body must be an empty JSON object")
                self._json(HTTPStatus.ACCEPTED, self.runtime.projects.rerun_segment(segments[2], segments[4]))
                return
            if len(segments) == 4 and segments[:2] == ["api", "jobs"] and segments[3] == "resume":
                self._resume_job(segments[2])
                return
            if len(segments) == 4 and segments[:2] == ["api", "jobs"] and segments[3] == "cancel":
                if self.headers.get("Content-Length", "0") != "0":
                    self._read_json()
                self._cancel_job(segments[2])
                return
            if len(segments) == 4 and segments[:2] == ["api", "jobs"] and segments[3] == "assets":
                self._import_job_output(segments[2])
                return
            if len(segments) == 4 and segments[:2] == ["api", "derivations"] and segments[3] == "assets":
                self._save_derivation(segments[2])
                return
            if path == "/api/maintenance/gc":
                self._garbage_collect()
                return
            raise ApiError(404, "not_found", "endpoint does not exist")
        except ApiError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.log_error("unhandled POST error: %r", error)
            self._error(ApiError(500, "internal_error", "internal server error"))

    def do_PUT(self) -> None:
        try:
            self._require_origin()
            self._require_auth()
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            segments = [segment for segment in path.split("/") if segment]
            if len(segments) == 3 and segments[:2] == ["api", "video-projects"]:
                self._json(HTTPStatus.OK, self.runtime.projects.update(
                    segments[2], self._read_json(maximum=self.runtime.config.max_project_json_bytes)
                ))
                return
            raise ApiError(404, "not_found", "endpoint does not exist")
        except ApiError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.log_error("unhandled PUT error: %r", error)
            self._error(ApiError(500, "internal_error", "internal server error"))

    def do_PATCH(self) -> None:
        try:
            self._require_origin()
            self._require_auth()
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            segments = [segment for segment in path.split("/") if segment]
            if len(segments) == 3 and segments[:2] == ["api", "assets"]:
                data = self._read_json()
                if not data or set(data) - {"display_name", "folder_id", "pinned"}:
                    raise ApiError(400, "invalid_parameter", "supply display_name, folder_id and/or pinned")
                folder_marker: Any = ...
                if "folder_id" in data:
                    folder_marker = data["folder_id"]
                name_marker: Any = data["display_name"] if "display_name" in data else ...
                pinned_marker: Any = data["pinned"] if "pinned" in data else ...
                with self.runtime.mutation_lock:
                    asset = self.runtime.assets.update_library_metadata(
                        segments[2], display_name=name_marker, folder_id=folder_marker, pinned=pinned_marker,
                        folder_exists=self.runtime.folders.get,
                    )
                self._json(HTTPStatus.OK, self.runtime.assets.public_metadata(asset))
                return
            if len(segments) == 3 and segments[:2] == ["api", "jobs"]:
                data = self._read_json()
                if set(data) != {"pinned"} or not isinstance(data.get("pinned"), bool):
                    raise ApiError(400, "invalid_parameter", "pinned must be supplied as a boolean")
                job_id = validate_id(segments[2], "job id")
                with self.runtime.mutation_lock:
                    job = self.runtime.jobs.get(job_id)
                    if not (job.get("status") == "completed" and self._visual_output_type(job) in {"image", "video"} and job.get("outputs")):
                        raise ApiError(409, "result_not_ready", "only completed media results can be pinned")
                    job["pinned"] = data["pinned"]
                    job["metadata_updated_at"] = time.time()
                    self.runtime.jobs.put(job_id, job)
                self._json(HTTPStatus.OK, self._listed_payload(job, summary=True))
                return
            if len(segments) == 3 and segments[:2] == ["api", "derivations"]:
                data = self._read_json()
                if set(data) != {"pinned"}:
                    raise ApiError(400, "invalid_parameter", "pinned must be supplied")
                self._json(HTTPStatus.OK, self.runtime.media.update_metadata(segments[2], pinned=data.get("pinned")))
                return
            if len(segments) == 3 and segments[:2] == ["api", "asset-folders"]:
                data = self._read_json()
                if not data or set(data) - {"name", "parent_id"}:
                    raise ApiError(400, "invalid_parameter", "supply name and/or parent_id")
                marker: Any = data["parent_id"] if "parent_id" in data else ...
                if "name" in data and data["name"] is None:
                    raise ApiError(400, "invalid_folder_name", "folder name must be a string")
                with self.runtime.mutation_lock:
                    folder = self.runtime.folders.update(segments[2], name=data.get("name"), parent_id=marker)
                self._json(HTTPStatus.OK, folder)
                return
            raise ApiError(404, "not_found", "endpoint does not exist")
        except ApiError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.log_error("unhandled PATCH error: %r", error)
            self._error(ApiError(500, "internal_error", "internal server error"))

    def do_DELETE(self) -> None:
        try:
            self._require_origin()
            self._require_auth()
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            segments = [segment for segment in path.split("/") if segment]
            if len(segments) == 3 and segments[:2] == ["api", "assets"]:
                self._delete_asset(segments[2])
                return
            if len(segments) == 3 and segments[:2] == ["api", "jobs"]:
                self._delete_job(segments[2])
                return
            if len(segments) == 4 and segments[:3] == ["api", "voice", "tasks"]:
                self._json(HTTPStatus.OK, self.runtime.voice.delete(segments[3]))
                return
            if len(segments) == 3 and segments[:2] == ["api", "asset-folders"]:
                folder_id = validate_id(segments[2], "folder id")
                with self.runtime.mutation_lock:
                    folder = self.runtime.folders.get(folder_id)
                    parent_id = folder.get("parent_id") if isinstance(folder.get("parent_id"), str) else None
                    folders = self.runtime.folders.list()
                    children = [item for item in folders if item.get("parent_id") == folder_id]
                    target_names = {
                        str(item.get("name", "")).casefold()
                        for item in folders
                        if item.get("id") != folder_id and item.get("parent_id") == parent_id and item.get("id") not in {child.get("id") for child in children}
                    }
                    if any(str(child.get("name", "")).casefold() in target_names for child in children):
                        raise ApiError(409, "folder_name_conflict", "a child folder conflicts with a folder at the destination level")
                    assets = [item for item in self.runtime.assets.list() if item.get("folder_id") == folder_id]
                    for asset in assets:
                        self.runtime.assets.update_library_metadata(str(asset["id"]), folder_id=parent_id, folder_exists=self.runtime.folders.get)
                    for child in children:
                        self.runtime.folders.update(str(child["id"]), parent_id=parent_id)
                    self.runtime.folders.delete(folder_id)
                self._json(HTTPStatus.OK, {
                    "id": folder_id, "deleted": True,
                    "assets_moved": len(assets), "subfolders_moved": len(children), "destination_folder_id": parent_id,
                })
                return
            if len(segments) == 3 and segments[:2] == ["api", "derivations"]:
                value = self.runtime.media.delete(segments[2])
                self._json(HTTPStatus.OK, {"id": value["id"], "deleted": True})
                return
            if len(segments) == 3 and segments[:2] == ["api", "video-projects"]:
                self._json(HTTPStatus.OK, self.runtime.projects.delete(segments[2]))
                return
            raise ApiError(404, "not_found", "endpoint does not exist")
        except ApiError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.log_error("unhandled DELETE error: %r", error)
            self._error(ApiError(500, "internal_error", "internal server error"))

    def do_HEAD(self) -> None:
        previous = getattr(self, "_head_only", False)
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = previous

    def do_GET(self) -> None:
        try:
            self._require_origin()
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path in {"/api/health", "/health"}:
                try:
                    self.runtime.comfy.health()
                    self._json(HTTPStatus.OK, {"status": "ok", "comfyui": "ok"})
                except ApiError as error:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "degraded", **error.as_dict()})
                return

            self._require_auth()
            query = urllib.parse.parse_qs(parsed.query)
            if path in {"/api/capabilities", "/capabilities"}:
                capabilities = self.runtime.comfy.capabilities(self.runtime.config, self.runtime.registry)
                video_capability = capabilities.get("video")
                if isinstance(video_capability, dict):
                    video_capability["character_migration"] = character_migration_capability(
                        profiles=[item for item in capabilities.get("profiles", []) if isinstance(item, dict)],
                        motion_context=video_capability.get("motion_context", {}) if isinstance(video_capability.get("motion_context"), dict) else {},
                    )
                    video_capability["replication"] = replication_capability(
                        profiles=[item for item in capabilities.get("profiles", []) if isinstance(item, dict)],
                        motion_context=video_capability.get("motion_context", {}) if isinstance(video_capability.get("motion_context"), dict) else {},
                    )
                safety_policy = public_safety_policy()
                safety_policy["risk_threshold"] = self.runtime.config.h3_token_risk_threshold
                self._json(HTTPStatus.OK, {
                    **capabilities,
                    "h3_reference_safety_policy": safety_policy,
                    "voice": self.runtime.voice.capabilities(),
                    "gpu_resources": self.runtime.resources.snapshot(),
                })
                return
            if path == "/api/resources/gpus":
                self._json(HTTPStatus.OK, {"gpus": [self.runtime.resources.snapshot()]})
                return
            if path == "/api/voice/capabilities":
                self._json(HTTPStatus.OK, self.runtime.voice.capabilities())
                return
            if path == "/api/voice/tasks":
                self._json(HTTPStatus.OK, self.runtime.voice.list())
                return
            if path == "/api/workflows/director":
                self._json(HTTPStatus.OK, director_workflow_index())
                return
            if path == "/api/assets":
                raw_folder = query.get("folder_id", [None])[0]
                folder_filter: Any = ...
                if raw_folder is not None:
                    folder_filter = None if raw_folder in {"", "root", "none"} else validate_id(raw_folder, "folder id")
                self._json(HTTPStatus.OK, {"assets": self.runtime.assets.list_public(
                    query=query.get("q", [""])[0], folder_id=folder_filter,
                )})
                return
            if path == "/api/asset-folders":
                self._json(HTTPStatus.OK, {"folders": self.runtime.folders.search(query.get("q", [""])[0])})
                return
            if path == "/api/derivations":
                self._json(HTTPStatus.OK, {"derivations": self.runtime.media.list_public()})
                return
            if path == "/api/jobs":
                raw_limit = query.get("limit", ["20"])[0]
                raw_cursor = query.get("cursor", [None])[0]
                try:
                    limit = int(raw_limit)
                except ValueError as error:
                    raise ApiError(400, "invalid_pagination", "limit must be an integer") from error
                if not 1 <= limit <= 100:
                    raise ApiError(400, "invalid_pagination", "limit must be 1..100")
                cursor = None if raw_cursor in {None, "", "0"} else str(raw_cursor)
                results_only = query.get("results", [""])[0] == "1"
                summary = query.get("summary", [""])[0] == "1"
                jobs, next_cursor = self._listed_jobs(limit=limit, cursor=cursor, results_only=results_only, summary=summary)
                include_pinned = query.get("include_pinned", [""])[0] == "1" and results_only and cursor is None
                self._json(
                    HTTPStatus.OK,
                    {
                        "jobs": jobs, "limit": limit, "next_cursor": next_cursor,
                        "instance_id": self.runtime.instance_id,
                        **({"pinned_jobs": self._pinned_results(summary=summary)} if include_pinned else {}),
                    },
                    cache_control="private, no-cache" if summary else "no-store",
                    etag=summary,
                )
                return
            if path == "/api/video-projects":
                self._json(HTTPStatus.OK, {"projects": self.runtime.projects.list()})
                return
            if path in {"/api/status", "/status"}:
                job_id = query.get("id", [""])[0]
                self._json(HTTPStatus.OK, self._job_status(job_id))
                return
            if path in {"/api/result", "/result"}:
                self._result(query.get("id", [""])[0])
                return
            if path in {"/api/download", "/download"}:
                raw_index = query.get("index", ["0"])[0]
                try:
                    index = int(raw_index)
                except ValueError as error:
                    raise ApiError(400, "output_index", "index must be an integer") from error
                output_path, _ = self._output_path(query.get("id", [""])[0], index)
                self._send_file(output_path, download=True)
                return
            if path == "/api/preview":
                raw_index = query.get("index", ["0"])[0]
                try:
                    index = int(raw_index)
                except ValueError as error:
                    raise ApiError(400, "output_index", "index must be an integer") from error
                output_path, _ = self._output_path(query.get("id", [""])[0], index)
                self._send_file(output_path, download=False)
                return

            segments = [segment for segment in path.split("/") if segment]
            if len(segments) == 4 and segments[:3] == ["api", "voice", "tasks"]:
                self._json(HTTPStatus.OK, self.runtime.voice.get(segments[3]))
                return
            if len(segments) == 5 and segments[:3] == ["api", "voice", "tasks"] and segments[4] in {"preview", "download"}:
                track_values = query.get("track", ["mix"])
                if len(track_values) != 1:
                    raise ApiError(400, "voice_track_invalid", "provide one voice output track")
                track = track_values[0]
                download = segments[4] == "download"
                track_path = self.runtime.voice.output_path(segments[3], track)
                self._send_file(
                    track_path, download=download,
                    original_name=f"{segments[3]}-{track_path.name}",
                    cache_control="private, no-cache",
                )
                return
            if len(segments) == 3 and segments[:2] == ["api", "media-tasks"]:
                self._json(HTTPStatus.OK, self.runtime.media_tasks.get(segments[2]))
                return
            if len(segments) == 4 and segments[:3] == ["api", "workflows", "director"]:
                preset = director_workflow_preset(segments[3])
                if query.get("download", [""])[0] == "1":
                    self._json_attachment(HTTPStatus.OK, preset, f"h3-director-{segments[3]}.json")
                else:
                    self._json(HTTPStatus.OK, preset)
                return
            if len(segments) == 3 and segments[:2] == ["api", "video-projects"]:
                self._json(HTTPStatus.OK, self.runtime.projects.get(segments[2]))
                return
            if len(segments) == 4 and segments[:2] == ["api", "jobs"] and segments[3] == "thumbnail":
                raw_index = query.get("index", ["0"])[0]
                try:
                    index = int(raw_index)
                except ValueError as error:
                    raise ApiError(400, "output_index", "index must be an integer") from error
                self._send_file(self._job_thumbnail(segments[2], index), download=False, original_name="thumbnail.jpg")
                return
            if len(segments) == 4 and segments[:2] == ["api", "jobs"] and segments[3] == "workflow":
                self._send_compiled_workflow(
                    segments[2], download=query.get("download", [""])[0] == "1",
                )
                return
            if len(segments) == 4 and segments[:2] == ["api", "assets"] and segments[3] == "thumbnail":
                self._send_file(self._asset_thumbnail(segments[2]), download=False, original_name="thumbnail.jpg")
                return
            if len(segments) == 3 and segments[:2] == ["api", "derivations"]:
                self._json(HTTPStatus.OK, self.runtime.media.public(self.runtime.media.get(segments[2])))
                return
            if len(segments) == 4 and segments[:2] == ["api", "derivations"] and segments[3] in {"content", "download", "thumbnail"}:
                value = self.runtime.media.get(segments[2])
                source = self.runtime.media.path(value)
                if segments[3] == "thumbnail":
                    source = self.runtime.media.thumbnail(source, cache_key=f"derivation:{value['id']}:{value['sha256']}", kind=str(value["kind"]))
                self._send_file(source, download=segments[3] == "download", original_name=str(value.get("display_name", source.name)))
                return
            if len(segments) == 5 and segments[:2] == ["api", "video-projects"] and segments[3] == "merged" and segments[4] in {"preview", "download", "thumbnail"}:
                output_path, _ = self.runtime.projects.merged_path(segments[2])
                if segments[4] == "thumbnail":
                    output_path = self.runtime.media.thumbnail(
                        output_path, cache_key=f"merged:{segments[2]}:{output_path.stat().st_mtime_ns}", kind="video",
                    )
                self._send_file(
                    output_path,
                    download=segments[4] == "download",
                    original_name=f"{segments[2]}-merged.mp4",
                    cache_control="private, no-cache",
                )
                return
            if len(segments) == 3 and segments[:2] == ["api", "assets"]:
                asset = self.runtime.assets.get(segments[2])
                self._json(HTTPStatus.OK, asset)
                return
            if len(segments) == 4 and segments[:2] == ["api", "assets"] and segments[3] == "content":
                asset = self.runtime.assets.get(segments[2])
                self._send_file(
                    self.runtime.assets.content_path(asset),
                    download=False,
                    original_name=str(asset.get("filename", "asset")),
                )
                return
            if len(segments) == 3 and segments[:2] == ["api", "jobs"]:
                self._json(HTTPStatus.OK, self._job_status(segments[2]))
                return
            if path == "/":
                self._json(
                    HTTPStatus.OK,
                    {
                        "name": "MiniMax H3 Video Studio API",
                        "version": 1,
                        "endpoints": {
                            "health": "/api/health",
                            "capabilities": "/api/capabilities",
                            "director_workflows": "/api/workflows/director",
                            "compiled_workflow": "/api/jobs/:id/workflow?download=1",
                            "upload": "POST /api/assets",
                            "assets": "GET /api/assets?q=...&folder_id=...",
                            "asset_folders": "GET|POST /api/asset-folders",
                            "generate": "POST /api/generate",
                            "status": "/api/status?id=...",
                            "result": "/api/result?id=...",
                            "download": "/api/download?id=...",
                            "thumbnail": "/api/jobs/:id/thumbnail?index=0",
                            "derive_media": "POST /api/media/derive",
                            "mux_audio": "POST /api/media/mux-audio",
                            "prepare_h3_reference": "POST /api/media/derive operation=prepare_h3_reference",
                            "character_migration_plan": "POST /api/video/character-migration/plan",
                            "replication_plan": "POST /api/video/replication/plan",
                            "media_task": "GET /api/media-tasks/:id; POST /api/media-tasks/:id/cancel",
                            "voice": "POST /api/voice/tasks; GET /api/voice/tasks/:id",
                            "gpu_resources": "GET /api/resources/gpus",
                            "derivations": "GET /api/derivations",
                            "analyze_scenes": "POST /api/media/analyze-scenes",
                            "save_derivation": "POST /api/derivations/:id/assets",
                        },
                    },
                )
                return
            raise ApiError(404, "not_found", "endpoint does not exist")
        except ApiError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.log_error("unhandled GET error: %r", error)
            self._error(ApiError(500, "internal_error", "internal server error"))


def create_server(config: Config | None = None) -> H3StudioServer:
    config = config or Config.from_env()
    config.prepare()
    jobs = JobStore(config.data_root / "metadata" / "jobs")
    comfy = ComfyClient(config.comfy_url)
    for job in jobs.list():
        if job.get("status") == "submitting":
            recovered_prompt = None
            if isinstance(job.get("client_id"), str):
                try:
                    recovered_prompt = comfy.find_prompt_by_client_id(str(job["client_id"]))
                except ApiError:
                    recovered_prompt = None
            if job.get("prompt_id") or recovered_prompt:
                job.update({
                    "prompt_id": job.get("prompt_id") or recovered_prompt,
                    "status": "queued", "message": "recovered after server restart",
                    "updated_at": time.time(),
                })
                jobs.put(str(job["id"]), job)
            elif time.time() - float(job.get("submission_started_at", job.get("created_at", 0)) or 0) >= config.submit_reconcile_grace_seconds:
                job.update({
                    "status": "failed",
                    "message": "ComfyUI submission could not be verified after the recovery grace period; it was not resubmitted",
                    "updated_at": time.time(),
                })
                jobs.put(str(job["id"]), job)
    runtime = Runtime(
        config=config,
        assets=AssetStore(config),
        jobs=jobs,
        comfy=comfy,
        registry=ProfileRegistry.load(config.data_root / "profiles"),
    )
    return H3StudioServer((config.host, config.port), Handler, runtime)


def main() -> None:
    config = Config.from_env()
    if not config.api_key:
        print("WARNING: H3_STUDIO_API_KEY is empty; API authentication is disabled")
    server = create_server(config)
    print(f"MiniMax H3 Video Studio API listening on http://{config.host}:{config.port}; ComfyUI={config.comfy_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
