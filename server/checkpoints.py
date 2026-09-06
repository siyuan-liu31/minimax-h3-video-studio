"""Durable, single-latest checkpoint storage for resumable H3 sampling."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ApiError
from .profiles import ProfileRegistry, WorkflowProfile
from .security import secure_join, validate_id
from .storage import AssetStore, JobStore, JsonStore
from .workflows import AssetRef, GenerationSpec


CHECKPOINT_FORMAT = "h3-sampling-checkpoint/v1"


def _find_latent_output(record: dict[str, Any]) -> dict[str, str] | None:
    found: list[dict[str, str]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            filename = value.get("filename")
            if isinstance(filename, str) and filename.lower().endswith(".latent"):
                found.append({
                    "filename": filename,
                    "subfolder": str(value.get("subfolder", "")),
                    "type": str(value.get("type", "output")),
                })
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    outputs = record.get("outputs", {})
    # Node 19 is the reviewed H3StudioSaveLatent node in every resumable H3 graph.
    # Do not accept an unrelated latent emitted by another node.
    visit(outputs.get("19") if isinstance(outputs, dict) else None)
    return found[-1] if found else None


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CheckpointManager:
    def __init__(
        self, config: Config, jobs: JobStore, assets: AssetStore,
        registry: ProfileRegistry, mutation_lock: threading.RLock,
    ) -> None:
        self.config = config
        self.jobs = jobs
        self.assets = assets
        self.registry = registry
        self.mutation_lock = mutation_lock
        self.metadata = JsonStore(config.data_root / "metadata" / "checkpoints")
        self.root = config.data_root / "checkpoints"
        # Keep latent staging outside AssetStore.upload_root (comfy_input/h3-studio).
        # Asset cleanup and duplicate scans must never see checkpoint internals.
        self.input_root = config.comfy_input / "h3-studio-checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)
        self.input_root.mkdir(parents=True, exist_ok=True)
        self._capture_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.garbage_collect()

    @staticmethod
    def profile_policy(profile: WorkflowProfile) -> dict[str, Any]:
        policy = profile.resume if isinstance(profile.resume, dict) else {}
        return policy if policy.get("supported") is True else {}

    def start_gc(self) -> None:
        if self.config.checkpoint_gc_seconds <= 0 or self._thread is not None:
            return
        self._stop.clear()

        def run() -> None:
            while not self._stop.wait(self.config.checkpoint_gc_seconds):
                try:
                    self.garbage_collect()
                except Exception:
                    continue

        self._thread = threading.Thread(target=run, name="h3-checkpoint-gc", daemon=True)
        self._thread.start()

    def stop_gc(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2)
        self._thread = None

    def fingerprint(self, job: dict[str, Any]) -> str:
        parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
        evidence = job.get("workflow_evidence") if isinstance(job.get("workflow_evidence"), dict) else {}
        references = job.get("references") if isinstance(job.get("references"), list) else []
        value = {
            "profile": [parameters.get("profile_id"), parameters.get("profile_version"), parameters.get("profile_digest")],
            "prompt_sha256": hashlib.sha256(str(job.get("prompt", "")).encode()).hexdigest(),
            "seed": parameters.get("seed"),
            "sampling": [parameters.get("sampler"), parameters.get("scheduler"), parameters.get("denoise")],
            "shape": [parameters.get("width"), parameters.get("height"), parameters.get("frames")],
            "models": [evidence.get("diffusion_model"), evidence.get("lora"), evidence.get("lora_strength")],
            "references": [
                [item.get("asset_id"), item.get("content_hash"), item.get("role"), item.get("include_audio")]
                for item in references if isinstance(item, dict)
            ],
        }
        return _canonical_hash(value)

    def capture(self, job: dict[str, Any], record: dict[str, Any]) -> dict[str, Any] | None:
        # Concurrent status polls may observe the same completed prompt.  Only
        # one of them may swap the chain manifest and retire its predecessor.
        with self._capture_lock:
            return self._capture(job, record)

    def _capture(self, job: dict[str, Any], record: dict[str, Any]) -> dict[str, Any] | None:
        policy = self._policy_for_job(job)
        if not policy:
            return None
        chain_id = validate_id(str(job.get("chain_id") or job.get("id")), "checkpoint chain id")
        job_id = validate_id(str(job.get("id")), "job id")
        try:
            current = self.metadata.get(chain_id)
        except ApiError as error:
            if error.status != 404:
                raise
            current = None
        if current and current.get("latest_job_id") == job_id:
            return current
        output = _find_latent_output(record)
        if output is None:
            raise ApiError(409, "checkpoint_missing", "ComfyUI completed without the required latent checkpoint")
        if output.get("type") != "output":
            raise ApiError(409, "checkpoint_invalid", "ComfyUI returned an unsupported checkpoint location")
        source = secure_join(self.config.comfy_output, output.get("subfolder", ""), output["filename"])
        if not source.is_file() or source.stat().st_size <= 0:
            raise ApiError(409, "checkpoint_missing", "ComfyUI checkpoint file is unavailable")
        checkpoint_id = uuid.uuid4().hex
        destination = secure_join(self.root, checkpoint_id + ".latent")
        temporary = destination.with_name(f"{checkpoint_id}.tmp-{uuid.uuid4().hex}.latent")
        created = time.time()
        try:
            shutil.copy2(source, temporary)
            with temporary.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            size = temporary.stat().st_size
            os.replace(temporary, destination)
            manifest = {
                "id": chain_id,
                "chain_id": chain_id,
                "format": CHECKPOINT_FORMAT,
                "checkpoint_id": checkpoint_id,
                "stored_name": destination.name,
                "sha256": digest,
                "size": size,
                "latest_job_id": job_id,
                "steps": int((job.get("parameters") or {}).get("steps", 0)),
                "max_total_steps": int(policy["max_total_steps"]),
                "schedule_version": policy["schedule_version"],
                "fingerprint": self.fingerprint(job),
                "created_at": created,
                "expires_at": created + self.config.checkpoint_ttl_hours * 3600,
            }
            with self.mutation_lock:
                self.metadata.put(chain_id, manifest)
                if current and isinstance(current.get("stored_name"), str) and current.get("stored_name") != destination.name:
                    secure_join(self.root, current["stored_name"]).unlink(missing_ok=True)
            return manifest
        except OSError as error:
            destination.unlink(missing_ok=True)
            if getattr(error, "errno", None) == 28:
                raise ApiError(507, "checkpoint_storage_full", "checkpoint storage is full") from error
            raise ApiError(500, "checkpoint_write_failed", "checkpoint could not be stored atomically") from error
        finally:
            temporary.unlink(missing_ok=True)

    def _policy_for_job(self, job: dict[str, Any]) -> dict[str, Any]:
        parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
        profile_id = parameters.get("profile_id")
        if not isinstance(profile_id, str):
            return {}
        try:
            profile = self.registry.get(profile_id)
        except ApiError:
            return {}
        policy = self.profile_policy(profile)
        if not policy:
            return {}
        if not profile.accepts_identity(
            str(parameters.get("profile_version", "")),
            str(parameters.get("profile_digest", "")),
        ):
            return {}
        return policy

    def latest(self, job: dict[str, Any]) -> dict[str, Any]:
        chain_id = validate_id(str(job.get("chain_id") or job.get("id")), "checkpoint chain id")
        try:
            manifest = self.metadata.get(chain_id)
        except ApiError as error:
            if error.status == 404:
                raise ApiError(409, "checkpoint_missing", "the task chain has no completed checkpoint") from error
            raise
        if float(manifest.get("expires_at", 0) or 0) <= time.time():
            raise ApiError(410, "checkpoint_expired", "the latest checkpoint has expired")
        path = secure_join(self.root, str(manifest.get("stored_name", "")))
        if not path.is_file() or path.stat().st_size != int(manifest.get("size", -1)):
            raise ApiError(409, "checkpoint_corrupt", "the latest checkpoint file is missing or damaged")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != manifest.get("sha256"):
            raise ApiError(409, "checkpoint_corrupt", "the latest checkpoint file failed its integrity check")
        latest_job = self.jobs.get(validate_id(str(manifest.get("latest_job_id")), "latest job id"))
        if latest_job.get("status") != "completed":
            raise ApiError(409, "checkpoint_state_mismatch", "latest checkpoint does not belong to a completed task")
        if self.fingerprint(latest_job) != manifest.get("fingerprint"):
            raise ApiError(409, "checkpoint_state_mismatch", "task inputs no longer match the latest checkpoint")
        self._validate_references(latest_job)
        return manifest

    def _validate_references(self, job: dict[str, Any]) -> None:
        for reference in job.get("references", []) if isinstance(job.get("references"), list) else []:
            if not isinstance(reference, dict):
                continue
            asset_id = validate_id(str(reference.get("asset_id")), "reference asset id")
            asset = self.assets.get(asset_id)
            expected = reference.get("content_hash")
            if isinstance(expected, str) and asset.get("sha256") != expected:
                raise ApiError(409, "checkpoint_reference_changed", "a checkpoint reference no longer matches its original content")

    def stage_input(self, manifest: dict[str, Any], job_id: str) -> tuple[str, Path]:
        checkpoint_id = validate_id(str(manifest.get("checkpoint_id")), "checkpoint id")
        source = secure_join(self.root, str(manifest.get("stored_name")))
        filename = f"{validate_id(job_id, 'job id')}-{checkpoint_id}.latent"
        destination = secure_join(self.input_root, filename)
        temporary = destination.with_name(f"{filename}.tmp-{uuid.uuid4().hex}")
        try:
            shutil.copy2(source, temporary)
            with temporary.open("rb") as stream:
                staged_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            if staged_sha256 != manifest.get("sha256"):
                raise ApiError(409, "checkpoint_corrupt", "staged checkpoint failed its integrity check")
            os.replace(temporary, destination)
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise ApiError(507 if getattr(error, "errno", None) == 28 else 500, "checkpoint_stage_failed", "checkpoint could not be staged for ComfyUI") from error
        finally:
            temporary.unlink(missing_ok=True)
        return f"h3-studio-checkpoints/{filename}", destination

    @staticmethod
    def _number(parameters: dict[str, Any], key: str, fallback: Any) -> Any:
        return parameters.get(key, fallback)

    def build_spec(self, job: dict[str, Any], *, steps: int) -> GenerationSpec:
        parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
        evidence = job.get("workflow_evidence") if isinstance(job.get("workflow_evidence"), dict) else {}
        profile = self.registry.get(str(parameters.get("profile_id", "")))
        if not self.profile_policy(profile):
            raise ApiError(409, "resume_unsupported", "the current Profile does not support resumable sampling")
        if not profile.accepts_identity(
            str(parameters.get("profile_version", "")),
            str(parameters.get("profile_digest", "")),
        ):
            raise ApiError(409, "checkpoint_profile_changed", "the Profile identity changed after checkpoint creation")
        model_role = "ref_model" if profile.compiler == "h3_ref" else "fl_model"
        expected_model = profile.model_bindings.get(model_role, str(getattr(self.config, model_role)))
        if evidence.get("diffusion_model") != expected_model:
            raise ApiError(409, "checkpoint_model_changed", "the diffusion model changed after checkpoint creation")
        if evidence.get("lora") not in {None, ""} or float(evidence.get("lora_strength", 0) or 0) != 0:
            raise ApiError(409, "checkpoint_model_changed", "the checkpoint was not created by the supported no-LoRA graph")
        references: list[AssetRef] = []
        for value in job.get("references", []) if isinstance(job.get("references"), list) else []:
            if not isinstance(value, dict):
                continue
            asset = self.assets.get(validate_id(str(value.get("asset_id")), "reference asset id"))
            media = asset.get("media") if isinstance(asset.get("media"), dict) else {}
            references.append(AssetRef(
                asset_id=str(asset["id"]), kind=str(asset["kind"]), comfy_path=str(asset["comfy_path"]),
                role=str(value.get("role", "reference")), label=str(value.get("tag_label", "")),
                include_audio=value.get("include_audio") is True,
                duration=float(value.get("duration", media.get("duration", 0)) or 0),
                has_audio=media.get("has_audio") is True, fps=float(media.get("fps", 0) or 0),
                voice_speaker=str(value.get("voice_speaker", "")), voice_subject=int(value.get("voice_subject", 0) or 0),
            ))
        return GenerationSpec(
            output_type="video", prompt=str(job.get("prompt", "")), negative_prompt="",
            width=int(parameters.get("width", 0)), height=int(parameters.get("height", 0)),
            steps=steps, seed=int(parameters.get("seed", 0)), references=tuple(references),
            mode=str(parameters.get("mode", "ref2va")), prompt_mode=str(parameters.get("prompt_mode", "default")),
            duration=float(parameters.get("duration_requested", 0) or 0), frames=int(parameters.get("frames", 0)),
            lora_strength=float(parameters.get("lora_strength", 0) or 0),
            ref_image_size=str(parameters.get("ref_image_size", "match")),
            denoise=float(parameters.get("denoise", 1) or 1),
            profile_id=profile.id, profile_version=profile.version, profile_digest=profile.digest(),
            compiler=profile.compiler, sampling_mode=profile.sampling_mode,
            sampler=str(parameters.get("sampler", "")), scheduler=str(parameters.get("scheduler", "")),
            model_bindings=dict(profile.model_bindings),
            reference_duration_total=sum(item.duration for item in references),
            director_mode=str(job.get("director_mode") or parameters.get("director_mode") or ""),
            requested_director_mode=str(parameters.get("requested_director_mode", "auto")),
            source_asset_id=str(job.get("source_asset_id") or parameters.get("source_asset_id") or ""),
        )

    def status(self, job: dict[str, Any]) -> dict[str, Any]:
        policy = self._policy_for_job(job)
        parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
        base = {
            "supported": bool(policy),
            "current_steps": int(parameters.get("steps", 0) or 0),
            "max_total_steps": int(policy.get("max_total_steps", 0) or 0),
            "latest_job_id": str(job.get("id", "")),
        }
        if not policy:
            return {**base, "can_resume": False, "reason": "profile_not_tested"}
        chain_id = str(job.get("chain_id") or job.get("id"))
        if job.get("status") in {"submitting", "queued", "running"}:
            return {**base, "can_resume": False, "reason": "checkpoint_pending", "latest_job_id": job.get("id")}
        active = next((
            item for item in self.jobs.list()
            if str(item.get("chain_id") or item.get("id")) == chain_id
            and item.get("status") in {"submitting", "queued", "running"}
            and item.get("id") != job.get("id")
        ), None)
        if active:
            return {**base, "can_resume": False, "reason": "chain_busy", "latest_job_id": active.get("id")}
        try:
            manifest = self.metadata.get(validate_id(chain_id, "checkpoint chain id"))
        except ApiError:
            reason = "checkpoint_pending" if job.get("status") in {"submitting", "queued", "running"} else str(job.get("checkpoint_error") or "checkpoint_missing")
            return {**base, "can_resume": False, "reason": reason}
        expired = float(manifest.get("expires_at", 0) or 0) <= time.time()
        steps = int(manifest.get("steps", 0) or 0)
        stored = str(manifest.get("stored_name", ""))
        try:
            path = secure_join(self.root, stored)
            corrupt = not path.is_file() or path.stat().st_size != int(manifest.get("size", -1))
        except ApiError:
            corrupt = True
        latest_job_id = str(manifest.get("latest_job_id", ""))
        try:
            latest_job = self.jobs.get(validate_id(latest_job_id, "latest job id"))
            state_mismatch = latest_job.get("status") != "completed" or self.fingerprint(latest_job) != manifest.get("fingerprint")
        except ApiError:
            state_mismatch = True
        reason = (
            "checkpoint_expired" if expired else
            "checkpoint_corrupt" if corrupt else
            "checkpoint_state_mismatch" if state_mismatch else
            "max_steps_reached" if steps >= int(policy["max_total_steps"]) else None
        )
        return {
            **base,
            "can_resume": reason is None,
            "reason": reason,
            "current_steps": steps,
            "latest_job_id": latest_job_id,
            "checkpoint_created_at": manifest.get("created_at"),
            "checkpoint_expires_at": manifest.get("expires_at"),
        }

    def cleanup_staged(self, job: dict[str, Any]) -> None:
        value = job.get("checkpoint_input_staged")
        if isinstance(value, str):
            secure_join(self.input_root, value).unlink(missing_ok=True)

    def garbage_collect(self) -> dict[str, int]:
        now = time.time()
        jobs = self.jobs.list()
        known_chains = {str(job.get("chain_id") or job.get("id")) for job in jobs}
        known_job_ids = {str(job.get("id")) for job in jobs}
        active_chains = {
            str(job.get("chain_id") or job.get("id")) for job in jobs
            if job.get("status") in {"submitting", "queued", "running"}
        }
        removed_manifests = removed_files = removed_temporary = 0
        live_names: set[str] = set()
        with self.mutation_lock:
            for manifest in self.metadata.list():
                chain_id = str(manifest.get("chain_id", ""))
                stored = str(manifest.get("stored_name", ""))
                if chain_id in active_chains:
                    live_names.add(stored)
                    continue
                latest_job_id = str(manifest.get("latest_job_id", ""))
                if (
                    chain_id not in known_chains
                    or latest_job_id not in known_job_ids
                    or float(manifest.get("expires_at", 0) or 0) <= now
                ):
                    try:
                        self.metadata.delete(chain_id)
                        removed_manifests += 1
                    except ApiError:
                        pass
                    if stored:
                        path = secure_join(self.root, stored)
                        if path.is_file():
                            path.unlink(missing_ok=True)
                            removed_files += 1
                else:
                    live_names.add(stored)
            for path in self.root.iterdir():
                if not path.is_file():
                    continue
                if ".tmp-" in path.name:
                    path.unlink(missing_ok=True)
                    removed_temporary += 1
                elif path.name not in live_names:
                    path.unlink(missing_ok=True)
                    removed_files += 1
            active_inputs = {
                str(job.get("checkpoint_input_staged")) for job in jobs
                if job.get("status") in {"submitting", "queued", "running"} and isinstance(job.get("checkpoint_input_staged"), str)
            }
            for path in self.input_root.iterdir():
                if path.is_file() and path.name not in active_inputs:
                    path.unlink(missing_ok=True)
                    removed_temporary += 1
        return {"manifests": removed_manifests, "files": removed_files, "temporary_files": removed_temporary}
