"""Durable, sequential orchestration for videos longer than one H3 clip."""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .comfy import ComfyClient, find_outputs
from .comfy_tasks import ComfyTaskCoordinator
from .character_migration import RECIPE_TYPE as CHARACTER_MIGRATION_RECIPE_TYPE
from .character_migration import validate_recipe as validate_character_migration_recipe
from .config import Config
from .errors import ApiError
from .motion_context import MotionContextStore
from .profiles import H3_MAX_DURATION_SECONDS, H3_REFERENCE_CAPACITY, ProfileRegistry
from .replication import RECIPE_TYPE as REPLICATION_RECIPE_TYPE
from .replication import validate_recipe as validate_replication_recipe
from .replication import validate_execution as validate_replication_execution
from .security import secure_join, validate_id
from .storage import AssetStore, JobStore, JsonStore
from .workflows import MotionContextPlan, compile_workflow, parse_generation_request, workflow_evidence


# This is an abuse/body-complexity ceiling, not a duration limit. At H3's
# per-segment maximum it still permits more than four hours in one project.
MAX_SEGMENTS = 1000
MAX_SOURCE_FPS = 240.0
MAX_SOURCE_FRAMES = 2**53 - 1  # Exact integer timeline shared with JavaScript.
TERMINAL_JOBS = {"completed", "failed", "canceled"}
ACTIVE_JOBS = {"submitting", "queued", "running"}
CONTINUATIONS = {"none", "tail_frame", "previous_video", "motion_context"}
POLL_SECONDS = 0.1
SUBMIT_RECONCILE_INTERVAL_SECONDS = 2.0
LITERAL_REFERENCE_TAG = re.compile(r"<(?:Picture|Video|Audio)\s+\d+>", re.IGNORECASE)
STABLE_REFERENCE_ALIAS = re.compile(r"@\{([^{}]+)\}")


def validate_project_recipe(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_project_recipe", "recipe must be an object")
    recipe_type = value.get("type")
    if recipe_type == CHARACTER_MIGRATION_RECIPE_TYPE:
        return validate_character_migration_recipe(value)
    if recipe_type == REPLICATION_RECIPE_TYPE:
        return validate_replication_recipe(value)
    raise ApiError(400, "invalid_project_recipe", "recipe type is unsupported")


class MergeCanceled(Exception):
    """Internal cooperative-cancellation signal for a local merge."""


class GenerationStopped(Exception):
    """Internal signal that a durable job was canceled before paid submit."""

    def __init__(self, job_id: str) -> None:
        super().__init__("project stopped before generation submission")
        self.job_id = job_id


class VideoProjectManager:
    """Owns durable project state and one cooperative worker per project."""

    def __init__(
        self,
        config: Config,
        assets: AssetStore,
        jobs: JobStore,
        comfy: ComfyClient,
        registry: ProfileRegistry,
        mutation_lock: threading.RLock,
        *,
        command_runner: Callable[..., Any] = subprocess.run,
        comfy_tasks: ComfyTaskCoordinator | None = None,
        motion_contexts: MotionContextStore | None = None,
    ) -> None:
        self.config = config
        self.assets = assets
        self.jobs = jobs
        self.comfy = comfy
        self.registry = registry
        self.lock = mutation_lock
        self.store = JsonStore(config.data_root / "metadata" / "video-projects")
        self.command_runner = command_runner
        self.comfy_tasks = comfy_tasks
        self.motion_contexts = motion_contexts or MotionContextStore(config)
        self._workers: dict[str, threading.Thread] = {}
        self._merge_processes: dict[str, subprocess.Popen[str]] = {}
        self._merge_cancel_events: dict[str, threading.Event] = {}
        self.reconcile()

    # ---- public API -----------------------------------------------------

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        title, segments, storyboard, recipe = self._validate_definition(body)
        now = time.time()
        project_id = uuid.uuid4().hex
        project = {
            "id": project_id,
            "title": title,
            "status": "completed" if segments and all(segment.get("status") == "completed" for segment in segments) else "draft",
            "current_index": -1,
            "selected_segment_ids": [],
            "stop_requested": False,
            "created_at": now,
            "updated_at": now,
            "segments": segments,
            "merge_attempts": [],
        }
        if storyboard is not None:
            project["storyboard"] = storyboard
        if recipe is not None:
            project["recipe"] = recipe
        with self.lock:
            self.store.put(project_id, project)
        return self.receipt(project)

    def list(self) -> list[dict[str, Any]]:
        return [self.receipt(project) for project in self.store.list()]

    def get(self, project_id: str) -> dict[str, Any]:
        return self.receipt(self._get(project_id))

    def update(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        project_id = validate_id(project_id, "project id")
        title, requested, storyboard, recipe = self._validate_definition(body)
        with self.lock:
            project = self.store.get(project_id)
            if project.get("status") in {"running", "stopping", "merging"}:
                raise ApiError(409, "project_busy", "a running or merging project cannot be edited")
            old_by_id = {
                str(segment.get("id")): segment
                for segment in project.get("segments", [])
                if isinstance(segment, dict)
            }
            recipe_changed = project.get("recipe") != recipe
            rebuilt: list[dict[str, Any]] = []
            changed_indices: list[int] = []
            source_changed_indices: set[int] = set()
            continuation_range_changed_indices: set[int] = set()
            for index, segment in enumerate(requested):
                old = old_by_id.get(str(segment["id"]))
                media_changed = bool(old) and old.get("media_source") != segment.get("media_source")
                source_changed = bool(old) and old.get("source_range") != segment.get("source_range")
                continuation_range_changed = (
                    bool(old)
                    and old.get("continuation_range") != segment.get("continuation_range")
                )
                same = (
                    bool(old)
                    and old.get("index") == index
                    and old.get("kind", "generation") == segment.get("kind", "generation")
                    and old.get("request") == segment.get("request")
                    and old.get("continuation") == segment["continuation"]
                    and old.get("motion_context") == segment.get("motion_context")
                    and not media_changed
                    and not source_changed
                    and not continuation_range_changed
                )
                if same:
                    kept = dict(old)
                    kept["index"] = index
                    rebuilt.append(kept)
                else:
                    changed_indices.append(index)
                    if segment.get("kind") == "media":
                        segment.update({"status": "completed", "error": None})
                    elif source_changed:
                        source_changed_indices.add(index)
                        segment.update({"status": "stale", "error": "source range changed"})
                    elif continuation_range_changed:
                        continuation_range_changed_indices.add(index)
                        segment.update({"status": "stale", "error": "continuation range changed"})
                    else:
                        segment["status"] = "pending"
                    segment["attempts"] = list(old.get("attempts", [])) if old and segment.get("kind") != "media" else []
                    rebuilt.append(segment)
            if set(old_by_id) != {str(segment["id"]) for segment in requested} and not changed_indices and rebuilt:
                changed_indices.append(0)
            if recipe_changed and rebuilt:
                changed_indices = sorted(set(changed_indices) | set(range(len(rebuilt))))
                for segment in rebuilt:
                    if segment.get("kind") != "media":
                        segment.update({"status": "stale", "error": "project recipe changed"})
                        segment.pop("job_id", None)
            reclaim_indices: set[int] = set()
            old_segments = project.get("segments", [])
            for changed_index in changed_indices:
                reclaim_indices.add(changed_index)
                for downstream_index in range(changed_index + 1, len(old_segments)):
                    if old_segments[downstream_index].get("continuation") == "none":
                        break
                    reclaim_indices.add(downstream_index)
            removed_ids = set(old_by_id) - {str(segment["id"]) for segment in requested}
            reclaim_indices.update(
                int(segment.get("index", -1))
                for segment in old_segments
                if str(segment.get("id")) in removed_ids
            )
            if reclaim_indices:
                self._reclaim_segment_assets(project, reclaim_indices)
            for changed_index in changed_indices:
                for downstream in rebuilt[changed_index + 1 :]:
                    if downstream.get("continuation") == "none":
                        break
                    if (
                        changed_index in source_changed_indices
                        or changed_index in continuation_range_changed_indices
                        or downstream.get("status") != "pending"
                    ):
                        if changed_index in source_changed_indices:
                            reason = "upstream source range changed"
                        elif changed_index in continuation_range_changed_indices:
                            reason = "upstream continuation range changed"
                        else:
                            reason = "upstream segment definition changed"
                        downstream.update({"status": "stale", "error": reason})
                        downstream.pop("job_id", None)
            if changed_indices or removed_ids:
                project.pop("merged", None)
            unchanged_results = not changed_indices and not removed_ids
            preserved_status = (
                "completed" if rebuilt and all(segment.get("status") == "completed" for segment in rebuilt)
                else "partial" if any(segment.get("status") == "completed" for segment in rebuilt)
                else "draft"
            ) if unchanged_results else "draft"
            project.update({
                "title": title, "segments": rebuilt, "status": preserved_status,
                "current_index": -1, "selected_segment_ids": [],
                "stop_requested": False, "updated_at": time.time(),
            })
            if storyboard is None:
                project.pop("storyboard", None)
            else:
                project["storyboard"] = storyboard
            if recipe is None:
                project.pop("recipe", None)
            else:
                project["recipe"] = recipe
            self.store.put(project_id, project)
            self._prune_motion_contexts(project)
        return self.receipt(project)

    def edit_replication_segment(self, project_id: str, segment_id: str, body: Any) -> dict[str, Any]:
        """Edit one request atomically; normal update invalidates dependent takes."""
        project_id = validate_id(project_id, "project id")
        segment_id = validate_id(segment_id, "segment id")
        allowed = {"expected_updated_at", "prompt", "seed", "steps"}
        if not isinstance(body, dict) or set(body) - allowed or not (set(body) & {"prompt", "seed", "steps"}):
            raise ApiError(400, "invalid_segment_edit", "supply prompt, seed and/or steps with expected_updated_at")
        expected = body.get("expected_updated_at")
        if isinstance(expected, bool) or not isinstance(expected, (int, float)) or not math.isfinite(expected):
            raise ApiError(400, "invalid_segment_edit", "expected_updated_at is required")
        if "prompt" in body and (not isinstance(body["prompt"], str) or not body["prompt"].strip() or len(body["prompt"]) > 12000):
            raise ApiError(400, "invalid_segment_edit", "prompt must contain 1..12000 characters")
        with self.lock:
            project = self._get(project_id)
            if project.get("recipe", {}).get("type") != REPLICATION_RECIPE_TYPE:
                raise ApiError(400, "not_replication_project", "this action requires a replication project")
            if project.get("updated_at") != expected:
                raise ApiError(409, "project_changed", "project changed; reload before saving the segment")
            definition = {
                "title": project["title"], "recipe": project["recipe"],
                **({"storyboard": project["storyboard"]} if project.get("storyboard") else {}),
                "segments": [{key: segment[key] for key in (
                    "id", "kind", "continuation", "request", "source_range", "continuation_range", "motion_context", "media_source",
                ) if key in segment} for segment in project["segments"]],
            }
            definition = json.loads(json.dumps(definition))
            segment = next((item for item in definition["segments"] if item["id"] == segment_id), None)
            if segment is None:
                raise ApiError(404, "segment_not_found", "segment does not exist")
            request = segment["request"]
            if "prompt" in body:
                request["prompt"] = body["prompt"]
                request["prompt_mode"] = "preserve_tags_only"
            for field in ("seed", "steps"):
                if field in body:
                    if isinstance(body[field], bool) or not isinstance(body[field], int):
                        raise ApiError(400, "invalid_segment_edit", f"{field} must be an integer")
                    request["parameters"][field] = body[field]
            return self.update(project_id, definition)

    def run(self, project_id: str, segment_ids: Any = None) -> dict[str, Any]:
        project_id = validate_id(project_id, "project id")
        with self.lock:
            project = self.store.get(project_id)
            validate_replication_execution(project)
            if not project.get("segments"):
                raise ApiError(409, "project_empty", "project has no segments")
            selected = self._selected_segment_ids(project, segment_ids)
            worker = self._workers.get(project_id)
            if worker and worker.is_alive():
                active_selection = project.get("selected_segment_ids")
                if (
                    segment_ids is not None
                    and isinstance(active_selection, list)
                    and not set(selected).issubset({str(item) for item in active_selection})
                ):
                    raise ApiError(409, "project_busy", "project is already running a different segment selection")
                return self.receipt(project)
            segments = project["segments"]
            selected_set = set(selected)
            remaining = [
                index for index, segment in enumerate(segments)
                if segment["id"] in selected_set and segment.get("status") != "completed"
            ]
            status = "running" if remaining else (
                "completed" if all(segment.get("status") == "completed" for segment in segments)
                else "partial"
            )
            project.update({
                "status": status,
                "current_index": remaining[0] if remaining else -1,
                "selected_segment_ids": selected,
                "stop_requested": False,
                "updated_at": time.time(),
            })
            self.store.put(project_id, project)
            self._prune_motion_contexts(project)
            if remaining:
                self._start(project_id, self._run_project, remaining[0])
        return self.receipt(project)

    @staticmethod
    def _selected_segment_ids(project: dict[str, Any], value: Any) -> list[str]:
        segments = project.get("segments", [])
        available = {str(segment.get("id")) for segment in segments}
        if value is None:
            return [str(segment["id"]) for segment in segments]
        if not isinstance(value, list) or not value:
            raise ApiError(400, "invalid_segment_ids", "segment_ids must be a non-empty array")
        selected: list[str] = []
        seen: set[str] = set()
        for item in value:
            segment_id = validate_id(item, "segment id")
            if segment_id in seen:
                raise ApiError(400, "invalid_segment_ids", "segment_ids must be unique")
            if segment_id not in available:
                raise ApiError(404, "segment_not_found", "selected segment does not exist")
            selected.append(segment_id)
            seen.add(segment_id)
        # Expand the user's selection to the exact sequential dependency plan.
        # A continuation segment cannot be submitted before its unfinished
        # predecessor. Persist the expanded ordered plan so UI, restart
        # recovery and the worker all observe the same paid-work boundary.
        expanded = set(selected)
        index_by_id = {
            str(segment.get("id")): index for index, segment in enumerate(segments)
        }
        for segment_id in selected:
            cursor = index_by_id[segment_id]
            while cursor > 0 and segments[cursor].get("continuation") != "none":
                predecessor = segments[cursor - 1]
                if predecessor.get("status") == "completed":
                    break
                expanded.add(str(predecessor["id"]))
                cursor -= 1
        return [
            str(segment["id"])
            for segment in segments
            if str(segment.get("id")) in expanded
        ]

    def stop(self, project_id: str) -> dict[str, Any]:
        project_id = validate_id(project_id, "project id")
        active_job_id: str | None = None
        with self.lock:
            project = self.store.get(project_id)
            if project.get("status") == "merging":
                project.update({"status": "stopping", "stop_requested": True, "updated_at": time.time()})
                self.store.put(project_id, project)
                event = self._merge_cancel_events.get(project_id)
                if event:
                    event.set()
                process = self._merge_processes.get(project_id)
                if process and process.poll() is None:
                    self._signal_process_group(process, signal.SIGTERM)
                return self.receipt(project)
            if project.get("status") not in {"running", "stopping"}:
                project["stop_requested"] = True
                project["updated_at"] = time.time()
                self.store.put(project_id, project)
                return self.receipt(project)
            project.update({"status": "stopping", "stop_requested": True, "updated_at": time.time()})
            current = int(project.get("current_index", -1))
            segments = project.get("segments", [])
            if 0 <= current < len(segments):
                job_id = segments[current].get("job_id")
                if isinstance(job_id, str):
                    active_job_id = job_id
            self.store.put(project_id, project)
        if active_job_id:
            try:
                self._cancel_job(active_job_id)
            except Exception as error:
                with self.lock:
                    project = self.store.get(project_id)
                    project.update({
                        "status": "stopping", "stop_requested": True,
                        "stop_warning": str(error) or "ComfyUI cancellation could not be confirmed",
                        "updated_at": time.time(),
                    })
                    self.store.put(project_id, project)
        return self.get(project_id)

    def rerun_segment(self, project_id: str, segment_id: str) -> dict[str, Any]:
        project_id = validate_id(project_id, "project id")
        segment_id = validate_id(segment_id, "segment id")
        with self.lock:
            project = self.store.get(project_id)
            validate_replication_execution(project)
            worker = self._workers.get(project_id)
            if worker and worker.is_alive():
                raise ApiError(409, "project_busy", "stop the project before rerunning a segment")
            self._reclaim_orphaned_derived_assets(owner_project_id=project_id)
            segments = project.get("segments", [])
            index = next((i for i, item in enumerate(segments) if item.get("id") == segment_id), -1)
            if index < 0:
                raise ApiError(404, "segment_not_found", "segment does not exist")
            if segments[index].get("kind") == "media":
                raise ApiError(409, "media_segment_not_generatable", "direct media segments are already ready and cannot be regenerated")
            dependent_indices = [index]
            for downstream_index in range(index + 1, len(segments)):
                if segments[downstream_index].get("continuation") == "none":
                    break
                dependent_indices.append(downstream_index)
            self._reclaim_segment_assets(project, dependent_indices)
            segments[index].update({"status": "pending", "error": None})
            segments[index].pop("job_id", None)
            # Only a contiguous continuation chain depends on this output.
            for downstream in segments[index + 1 :]:
                if downstream.get("continuation") == "none":
                    break
                downstream.update({"status": "stale", "error": "upstream segment was rerun"})
                downstream.pop("job_id", None)
            project.pop("merged", None)
            project.update({
                "status": "running", "current_index": index,
                "selected_segment_ids": [segment_id],
                "stop_requested": False, "updated_at": time.time(),
            })
            self.store.put(project_id, project)
            self._prune_motion_contexts(project)
            # This endpoint is intentionally exact: a single click may spend
            # for only the selected segment.  Dependent clips stay stale until
            # the user explicitly runs them or the whole project.
            self._start(project_id, self._run_single_segment, index)
        return self.receipt(project)

    def delete(self, project_id: str) -> dict[str, Any]:
        """Delete an idle project and reclaim only its owned derived assets.

        Generated outputs and immutable job/hash evidence remain in Results;
        continuation copies are implementation artifacts and are safe to
        reclaim once no other project definition owns them.
        """
        project_id = validate_id(project_id, "project id")
        with self.lock:
            project = self.store.get(project_id)
            worker = self._workers.get(project_id)
            if (worker and worker.is_alive()) or project.get("status") in {"running", "stopping", "merging"}:
                raise ApiError(409, "project_busy", "stop the project before deleting it")
            reclaimed = self._reclaim_segment_assets(project, range(len(project.get("segments", []))), deleting=True)
            self._cleanup_merge_paths(project)
            reclaimed_contexts = self.motion_contexts.delete_project(project_id)
            self.store.delete(project_id)
            reclaimed += self._reclaim_orphaned_derived_assets(owner_project_id=project_id)
        return {
            "id": project_id,
            "deleted": True,
            "reclaimed_derived_assets": reclaimed,
            "reclaimed_motion_contexts": reclaimed_contexts,
        }

    def merge(self, project_id: str) -> dict[str, Any]:
        project_id = validate_id(project_id, "project id")
        with self.lock:
            project = self.store.get(project_id)
            validate_replication_execution(project)
            worker = self._workers.get(project_id)
            if worker and worker.is_alive():
                raise ApiError(409, "project_busy", "project generation or merge is still running")
            segments = project.get("segments", [])
            if not segments or any(segment.get("status") != "completed" for segment in segments):
                raise ApiError(409, "segments_not_ready", "all ordered segments must be completed and non-stale before merge")
            attempt = {"id": uuid.uuid4().hex, "status": "merging", "started_at": time.time()}
            project.setdefault("merge_attempts", []).append(attempt)
            project["merged"] = {"status": "merging"}
            project.update({"status": "merging", "updated_at": time.time()})
            self.store.put(project_id, project)
            self._merge_cancel_events[project_id] = threading.Event()
            self._start(project_id, self._merge_project, attempt["id"])
        return self.receipt(project)

    def merged_path(self, project_id: str) -> tuple[Path, dict[str, Any]]:
        project = self._get(project_id)
        merged = project.get("merged")
        if not isinstance(merged, dict) or merged.get("status") != "completed":
            raise ApiError(409, "merge_not_ready", "merged result is not ready")
        relative = merged.get("relative_path")
        if not isinstance(relative, str):
            raise ApiError(500, "metadata_corrupt", "merged result path is missing")
        path = secure_join(self.config.comfy_output, relative)
        if not path.is_file():
            raise ApiError(404, "output_file_missing", "merged output file is missing")
        return path, self._public_merged(project)

    def reconcile(self) -> None:
        """Resume persisted workers without ever resubmitting an existing attempt."""
        # A hard crash can happen after a continuation asset is imported but
        # before its attempt metadata is committed.  Sweep those unattached
        # owned artifacts on startup so repeated crashes cannot exhaust quota.
        with self.lock:
            self._reclaim_orphaned_derived_assets()
        for project in self.store.list():
            project_id = str(project.get("id", ""))
            if len(project_id) != 32:
                continue
            status = project.get("status")
            if status == "stopping" or (status == "running" and project.get("stop_requested")):
                if not self._reconcile_stopping(project):
                    self._start(project_id, self._retry_stopping)
            elif status == "running":
                current = int(project.get("current_index", -1))
                self._start(project_id, self._run_project, max(0, current))
            elif status == "merging":
                # A local ffmpeg process cannot survive a server restart. Its
                # attempt is failed explicitly; the user may safely retry.
                self._cleanup_merge_paths(project)
                merged = {"status": "failed", "error": "server restarted during merge; partial output was removed; retry merge"}
                project["merged"] = merged
                project["status"] = "failed"
                attempts = project.get("merge_attempts", [])
                if attempts and attempts[-1].get("status") == "merging":
                    attempts[-1].update({"status": "failed", "error": merged["error"], "finished_at": time.time()})
                project["updated_at"] = time.time()
                self.store.put(project_id, project)

    def _retry_stopping(self, project_id: str) -> None:
        """Retry an unconfirmed remote cancellation without lying about state."""
        while True:
            time.sleep(2)
            with self.lock:
                try:
                    project = self.store.get(project_id)
                except ApiError:
                    return
                if project.get("status") != "stopping":
                    return
            if self._reconcile_stopping(project):
                return

    # ---- validation / receipts ----------------------------------------

    def _validate_definition(
        self, body: Any,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_project", "project body must be an object")
        extra = set(body) - {"title", "segments", "storyboard", "recipe"}
        if extra:
            raise ApiError(400, "invalid_project", f"unsupported project fields: {', '.join(sorted(extra))}")
        title = body.get("title", "")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
            raise ApiError(400, "invalid_project", "title must contain 1..200 characters")
        raw_recipe = body.get("recipe")
        recipe = validate_project_recipe(raw_recipe) if raw_recipe is not None else None
        is_replication = bool(recipe and recipe.get("type") == REPLICATION_RECIPE_TYPE)
        raw_segments = body.get("segments")
        if not isinstance(raw_segments, list) or (not is_replication and len(raw_segments) > MAX_SEGMENTS):
            raise ApiError(400, "invalid_segments", f"segments must be an array of at most {MAX_SEGMENTS} items for ordinary projects")
        storyboard = self._validate_storyboard(
            body.get("storyboard"), max_cuts=max(0, len(raw_segments) - 1) if is_replication else 200,
        )
        include_source_audio = bool(recipe and recipe.get("audio_policy") == "reference-source")
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_segments):
            if not isinstance(raw, dict) or set(raw) - {
                "id", "kind", "continuation", "request", "source_range", "continuation_range",
                "media_source", "motion_context",
            }:
                raise ApiError(400, "invalid_segment", f"segment {index + 1} has unsupported fields")
            supplied_id = raw.get("id")
            segment_id = validate_id(supplied_id, "segment id") if isinstance(supplied_id, str) else uuid.uuid4().hex
            if segment_id in seen:
                raise ApiError(400, "invalid_segment", "segment ids must be unique")
            kind = str(raw.get("kind", "generation"))
            if kind not in {"generation", "media"}:
                raise ApiError(400, "invalid_segment_kind", "segment kind must be generation or media")
            if kind == "media":
                if any(raw.get(key) is not None for key in ("request", "source_range", "continuation_range", "motion_context")):
                    raise ApiError(400, "invalid_media_segment", "direct media segments cannot contain H3 request or reference fields")
                if str(raw.get("continuation", "none")) != "none":
                    raise ApiError(400, "invalid_media_segment", "direct media segments cannot use H3 continuation")
                media_source = self._validate_media_source(raw.get("media_source"))
                result.append({
                    "id": segment_id, "index": index, "kind": "media",
                    "continuation": "none", "status": "completed",
                    "media_source": media_source, "attempts": [],
                })
                seen.add(segment_id)
                continue
            if raw.get("media_source") is not None:
                raise ApiError(400, "invalid_generation_segment", "generation segments cannot contain media_source")
            continuation = str(raw.get("continuation", "none"))
            if continuation not in CONTINUATIONS or (index == 0 and continuation != "none"):
                raise ApiError(400, "invalid_continuation", "continuation must be none, tail_frame, previous_video, or motion_context; the first segment must use none")
            motion_context = self._validate_motion_context(raw.get("motion_context"), continuation)
            source_range = self._validate_source_range(raw.get("source_range"), storyboard)
            previous_expected_frames: int | None = None
            if index > 0:
                previous = result[index - 1]
                if previous.get("kind") == "media" and continuation != "none":
                    raise ApiError(400, "continuation_from_media", "H3 continuation cannot use a direct media clip as its generated predecessor")
                previous_duration = self._segment_duration(previous)
                if math.isfinite(previous_duration) and previous_duration > 0:
                    previous_expected_frames = int(round(previous_duration * 24.0))
            continuation_range = self._validate_continuation_range(
                raw.get("continuation_range"), continuation, previous_expected_frames,
            )
            request = self._validate_request(
                raw.get("request"), continuation, source_range, continuation_range,
                include_source_audio=include_source_audio,
                pad_source_reference=is_replication,
            )
            segment = {
                "id": segment_id, "index": index, "continuation": continuation,
                "status": "pending", "request": request, "attempts": [],
            }
            if source_range is not None:
                segment["source_range"] = source_range
            if continuation_range is not None:
                segment["continuation_range"] = continuation_range
            if motion_context is not None:
                segment["motion_context"] = motion_context
            result.append(segment)
            seen.add(segment_id)
        return title.strip(), result, storyboard, recipe

    @staticmethod
    def _validate_motion_context(value: Any, continuation: str) -> dict[str, int] | None:
        if continuation != "motion_context":
            if value is not None:
                raise ApiError(400, "motion_context_mode", "motion_context settings require motion_context continuation")
            return None
        if value is None:
            value = {}
        if not isinstance(value, dict) or set(value) - {"video_frames", "audio_frames"}:
            raise ApiError(400, "invalid_motion_context", "motion_context accepts only video_frames and audio_frames")
        video_frames = value.get("video_frames", 22)
        audio_frames = value.get("audio_frames", 24)
        if isinstance(video_frames, bool) or video_frames not in {5, 22, 39, 56}:
            raise ApiError(400, "invalid_motion_context", "video_frames must be 5, 22, 39, or 56")
        if isinstance(audio_frames, bool) or not isinstance(audio_frames, int) or not 0 <= audio_frames <= 240:
            raise ApiError(400, "invalid_motion_context", "audio_frames must be an integer from 0 through 240")
        return {"video_frames": int(video_frames), "audio_frames": audio_frames}

    @staticmethod
    def _finite_number(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise ApiError(400, "invalid_storyboard", f"{label} must be a finite positive number")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise ApiError(400, "invalid_storyboard", f"{label} must be a finite positive number") from error
        if not math.isfinite(parsed) or parsed <= 0 or parsed > MAX_SOURCE_FPS:
            raise ApiError(400, "invalid_storyboard", f"{label} must be a finite positive number")
        return parsed

    @staticmethod
    def _positive_frame(value: Any, label: str) -> int:
        if (
            isinstance(value, bool) or not isinstance(value, int)
            or value <= 0 or value > MAX_SOURCE_FRAMES
        ):
            raise ApiError(400, "invalid_storyboard", f"{label} must be a positive integer")
        return value

    def _asset_video_bounds(self, asset_id: str) -> tuple[dict[str, Any], float, int]:
        asset_id = validate_id(asset_id, "asset id")
        asset = self.assets.get(asset_id)
        if asset.get("kind") != "video":
            raise ApiError(400, "source_asset_not_video", "storyboard and source_range assets must be videos")
        media = asset.get("media")
        if not isinstance(media, dict):
            media = {}
        # Uploaded non-24fps videos retain their original file for download and
        # a single normalized Comfy input. Source ranges use the normalized
        # frame count, not the container duration (which can include audio tail).
        fps_value = media.get("reference_fps") or media.get("fps") if media.get("normalized_to_24fps") else media.get("fps")
        fps = self._finite_number(fps_value, "source video fps")
        duration_value = media.get("duration")
        try:
            duration = float(duration_value)
        except (TypeError, ValueError):
            duration = 0.0
        frame_count_value = media.get("frame_count")
        if not isinstance(frame_count_value, int) or frame_count_value <= 0:
            video_duration = media.get("video_duration") if media.get("normalized_to_24fps") else None
            try:
                duration = float(video_duration) if video_duration is not None else duration
            except (TypeError, ValueError):
                duration = 0.0
            if math.isfinite(duration) and duration > 0:
                frame_count_value = int(round(duration * fps))
        frame_count = self._positive_frame(frame_count_value, "source video frame_count")
        return asset, fps, frame_count

    @staticmethod
    def _segment_duration(segment: dict[str, Any]) -> float:
        media_source = segment.get("media_source")
        if segment.get("kind") == "media" and isinstance(media_source, dict):
            try:
                return (int(media_source["end_frame"]) - int(media_source["start_frame"])) / float(media_source["fps"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return 0.0
        request = segment.get("request") if isinstance(segment.get("request"), dict) else {}
        parameters = request.get("parameters") if isinstance(request.get("parameters"), dict) else {}
        try:
            return float(parameters.get("duration", 0))
        except (TypeError, ValueError):
            return 0.0

    def _validate_media_source(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_media_source", "direct media requires a media_source object")
        source_type = value.get("type")
        common = {"type", "start_frame", "end_frame", "fps", "keep_audio"}
        if source_type == "asset":
            allowed = common | {"asset_id"}
            if set(value) - allowed or "asset_id" not in value or "job_id" in value or "index" in value:
                raise ApiError(400, "invalid_media_source", "asset media_source requires only type, asset_id, frame range, fps, and keep_audio")
            asset_id = validate_id(value.get("asset_id"), "asset id")
            asset, actual_fps, frame_count = self._asset_video_bounds(asset_id)
            media = asset.get("media") if isinstance(asset.get("media"), dict) else {}
            identity: dict[str, Any] = {"type": "asset", "asset_id": asset_id}
        elif source_type == "job":
            allowed = common | {"job_id", "index"}
            if set(value) - allowed or "job_id" not in value or "asset_id" in value:
                raise ApiError(400, "invalid_media_source", "job media_source requires only type, job_id, optional index, frame range, fps, and keep_audio")
            job_id = validate_id(value.get("job_id"), "job id")
            output_index = value.get("index", 0)
            if isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
                raise ApiError(400, "invalid_media_source", "job output index must be a non-negative integer")
            path, output = self._job_output_at(self.jobs.get(job_id), output_index)
            media = output.get("media") if isinstance(output.get("media"), dict) else AssetStore._probe_media(path, "video")
            if media.get("has_video") is False:
                raise ApiError(400, "media_source_not_video", "direct media job output must be a video")
            actual_fps = self._finite_number(media.get("fps"), "direct media fps")
            frame_value = media.get("frame_count")
            duration_value = media.get("video_duration", media.get("duration"))
            try:
                duration = float(duration_value)
            except (TypeError, ValueError):
                duration = 0.0
            if not isinstance(frame_value, int) or isinstance(frame_value, bool) or frame_value <= 0:
                frame_value = int(round(duration * actual_fps)) if math.isfinite(duration) and duration > 0 else 0
            frame_count = self._positive_frame(frame_value, "direct media frame_count")
            identity = {"type": "job", "job_id": job_id, **({"index": output_index} if output_index else {})}
        else:
            raise ApiError(400, "invalid_media_source", "media_source type must be asset or job")

        fps = self._finite_number(value.get("fps"), "direct media fps")
        if not math.isclose(fps, actual_fps, rel_tol=0, abs_tol=0.01):
            raise ApiError(400, "media_source_fps_mismatch", "media_source fps must match the source video")
        for key in ("start_frame", "end_frame"):
            if isinstance(value.get(key), bool) or not isinstance(value.get(key), int):
                raise ApiError(400, "invalid_media_source", f"{key} must be an integer")
        start_frame = int(value["start_frame"])
        end_frame = int(value["end_frame"])
        if start_frame < 0 or end_frame <= start_frame or end_frame > frame_count:
            raise ApiError(400, "media_source_bounds", "direct media range must be inside the source video")
        keep_audio = value.get("keep_audio", bool(media.get("has_audio")))
        if not isinstance(keep_audio, bool):
            raise ApiError(400, "invalid_media_source", "keep_audio must be boolean")
        if keep_audio and media.get("has_audio") is not True:
            raise ApiError(400, "media_source_audio", "keep_audio requires a source audio track")
        return {
            **identity, "start_frame": start_frame, "end_frame": end_frame,
            "fps": fps, "keep_audio": keep_audio,
        }

    def _validate_storyboard(self, value: Any, *, max_cuts: int = 200) -> dict[str, Any] | None:
        if value is None:
            return None
        required = {"source_asset_id", "fps", "frame_count", "cut_frames"}
        if not isinstance(value, dict) or set(value) != required:
            raise ApiError(400, "invalid_storyboard", "storyboard requires only source_asset_id, fps, frame_count, and cut_frames")
        source_asset_id = validate_id(value.get("source_asset_id"), "source asset id")
        _, source_fps, source_frame_count = self._asset_video_bounds(source_asset_id)
        fps = self._finite_number(value.get("fps"), "storyboard fps")
        frame_count = self._positive_frame(value.get("frame_count"), "storyboard frame_count")
        if not math.isclose(fps, source_fps, rel_tol=0, abs_tol=0.01) or frame_count != source_frame_count:
            raise ApiError(400, "storyboard_source_mismatch", "storyboard fps and frame_count must match the source video")
        cuts = value.get("cut_frames")
        if not isinstance(cuts, list) or len(cuts) > max_cuts:
            raise ApiError(400, "invalid_storyboard", f"cut_frames must be an array of at most {max_cuts} items")
        normalized_cuts: list[int] = []
        previous = 0
        for cut in cuts:
            if isinstance(cut, bool) or not isinstance(cut, int) or cut <= previous or cut >= frame_count:
                raise ApiError(400, "invalid_storyboard", "cut_frames must be strictly increasing integers inside the source video")
            normalized_cuts.append(cut)
            previous = cut
        return {
            "source_asset_id": source_asset_id, "fps": fps,
            "frame_count": frame_count, "cut_frames": normalized_cuts,
        }

    def _validate_source_range(
        self, value: Any, storyboard: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        required = {"asset_id", "start_frame", "end_frame", "fps"}
        if not isinstance(value, dict) or set(value) != required:
            raise ApiError(400, "invalid_source_range", "source_range requires only asset_id, start_frame, end_frame, and fps")
        asset_id = validate_id(value.get("asset_id"), "source asset id")
        _, source_fps, source_frame_count = self._asset_video_bounds(asset_id)
        fps = self._finite_number(value.get("fps"), "source_range fps")
        for key in ("start_frame", "end_frame"):
            if isinstance(value.get(key), bool) or not isinstance(value.get(key), int):
                raise ApiError(400, "invalid_source_range", f"{key} must be an integer")
        start_frame = int(value["start_frame"])
        end_frame = int(value["end_frame"])
        if storyboard is not None:
            if asset_id != storyboard["source_asset_id"]:
                raise ApiError(400, "source_range_asset_mismatch", "source_range must use the storyboard source asset")
            source_fps = float(storyboard["fps"])
            source_frame_count = int(storyboard["frame_count"])
        if not math.isclose(fps, source_fps, rel_tol=0, abs_tol=0.01):
            raise ApiError(400, "source_range_fps_mismatch", "source_range fps must match the source video")
        if start_frame < 0 or end_frame <= start_frame or end_frame > source_frame_count:
            raise ApiError(400, "source_range_bounds", "source_range must be a positive interval inside the source video")
        duration = (end_frame - start_frame) / fps
        if not math.isfinite(duration) or duration <= 0 or duration > H3_MAX_DURATION_SECONDS:
            raise ApiError(
                400, "source_range_duration",
                f"source_range duration must be greater than 0 and at most {H3_MAX_DURATION_SECONDS:g} seconds",
            )
        return {
            "asset_id": asset_id, "start_frame": start_frame,
            "end_frame": end_frame, "fps": fps,
        }

    @staticmethod
    def _validate_continuation_range(
        value: Any, continuation: str, previous_expected_frames: int | None,
    ) -> dict[str, Any] | None:
        """Normalize an exact interval from the immediately previous output."""
        if value is None:
            return None
        if continuation != "previous_video":
            raise ApiError(
                400, "continuation_range_mode",
                "continuation_range is only valid with previous_video continuation",
            )
        required = {"start_frame", "end_frame", "fps"}
        if not isinstance(value, dict) or set(value) != required:
            raise ApiError(
                400, "invalid_continuation_range",
                "continuation_range requires only start_frame, end_frame, and fps",
            )
        for key in ("start_frame", "end_frame"):
            if isinstance(value.get(key), bool) or not isinstance(value.get(key), int):
                raise ApiError(400, "invalid_continuation_range", f"{key} must be an integer")
        start_frame = int(value["start_frame"])
        end_frame = int(value["end_frame"])
        fps_value = value.get("fps")
        if isinstance(fps_value, bool):
            raise ApiError(400, "continuation_range_fps", "continuation_range fps must be 24")
        try:
            fps = float(fps_value)
        except (TypeError, ValueError) as error:
            raise ApiError(400, "continuation_range_fps", "continuation_range fps must be 24") from error
        if not math.isfinite(fps) or not math.isclose(fps, 24.0, rel_tol=0, abs_tol=1e-6):
            raise ApiError(400, "continuation_range_fps", "continuation_range fps must be 24")
        frame_count = end_frame - start_frame
        if start_frame < 0 or frame_count <= 0:
            raise ApiError(
                400, "continuation_range_bounds",
                "continuation_range must be a positive frame interval",
            )
        if frame_count > 360:
            raise ApiError(
                400, "continuation_range_duration",
                "continuation_range must contain at most 360 frames (15 seconds at 24 fps)",
            )
        if previous_expected_frames is None or end_frame > previous_expected_frames:
            raise ApiError(
                400, "continuation_range_bounds",
                "continuation_range must be inside the previous segment's expected output",
            )
        return {"start_frame": start_frame, "end_frame": end_frame, "fps": 24.0}

    def _validate_request(
        self,
        value: Any,
        continuation: str,
        source_range: dict[str, Any] | None = None,
        continuation_range: dict[str, Any] | None = None,
        *,
        include_source_audio: bool = False,
        pad_source_reference: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_segment_request", "segment request must be an object")
        allowed = {
            "prompt", "parts", "parameters", "profile_id", "profile_version",
            "profile_digest", "references", "prompt_mode", "director_mode",
            "source_asset_id",
        }
        extra = set(value) - allowed
        if extra:
            raise ApiError(400, "invalid_segment_request", f"unsupported request fields: {', '.join(sorted(extra))}")
        profile_id = value.get("profile_id")
        version = value.get("profile_version")
        digest = value.get("profile_digest")
        if not all(isinstance(item, str) and item for item in (profile_id, version, digest)):
            raise ApiError(400, "profile_identity_required", "every segment must pin profile_id, profile_version, and profile_digest")
        profile = self.registry.get(str(profile_id))
        if profile.output_type != "video" or not profile.accepts_identity(str(version), str(digest)):
            raise ApiError(409, "profile_version_mismatch", "the pinned video workflow profile is unavailable or changed")
        prompt_mode = value.get("prompt_mode", "default")
        if not isinstance(prompt_mode, str) or prompt_mode not in {"default", "preserve_tags_only"}:
            raise ApiError(400, "invalid_parameter", "prompt_mode must be default or preserve_tags_only")
        prompt = value.get("prompt", "")
        parts = value.get("parts")
        if prompt_mode == "preserve_tags_only":
            if not isinstance(prompt, str) or not prompt.strip():
                raise ApiError(400, "invalid_parameter", "prompt is required in preserve_tags_only mode")
            parts = {}
        elif (not isinstance(prompt, str) or not prompt.strip()) and not isinstance(parts, dict):
            raise ApiError(400, "invalid_parameter", "prompt or prompt parts are required")
        parameters = value.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ApiError(400, "invalid_parameter", "parameters must be an object")
        references = value.get("references", [])
        if not isinstance(references, list):
            raise ApiError(400, "invalid_references", "references must be an array")
        normalized_refs: list[dict[str, Any]] = []
        reference_kinds: list[str] = []
        seen: set[str] = set()
        for item in references:
            if not isinstance(item, dict) or set(item) - {"id", "asset_id", "role", "include_audio", "voice_speaker", "voice_subject"}:
                raise ApiError(400, "invalid_references", "reference has unsupported fields")
            asset_id = item.get("asset_id", item.get("id"))
            if not isinstance(asset_id, str):
                raise ApiError(400, "invalid_references", "reference requires id or asset_id")
            validate_id(asset_id, "asset id")
            if asset_id in seen:
                raise ApiError(400, "invalid_references", "reference asset ids must be unique")
            asset = self.assets.get(asset_id)
            normalized = {"asset_id": asset_id, "role": str(item.get("role", "reference"))}
            for key in ("include_audio", "voice_speaker", "voice_subject"):
                if key in item:
                    normalized[key] = item[key]
            normalized_refs.append(normalized)
            reference_kinds.append(str(asset.get("kind", "")))
            seen.add(asset_id)
        pixel_continuation = continuation in {"tail_frame", "previous_video"}
        reserved_references = int(pixel_continuation) + int(source_range is not None)
        reference_limit = profile.limits.get("references", 0)
        if not isinstance(reference_limit, int) or len(normalized_refs) > reference_limit - reserved_references:
            raise ApiError(
                400, "too_many_references",
                f"source ranges and continuation inputs each reserve one of this profile's {reference_limit} reference slots",
            )
        modality_counts = {
            kind: sum(candidate == kind for candidate in reference_kinds)
            for kind in H3_REFERENCE_CAPACITY
        }
        modality_counts["image"] += int(continuation == "tail_frame")
        modality_counts["video"] += int(continuation == "previous_video") + int(source_range is not None)
        modality_counts["audio"] += sum(
            kind == "video" and reference.get("include_audio") is True
            for kind, reference in zip(reference_kinds, normalized_refs, strict=True)
        ) + int(source_range is not None and include_source_audio)
        for kind, maximum in H3_REFERENCE_CAPACITY.items():
            if modality_counts[kind] > maximum:
                raise ApiError(400, "too_many_references", f"H3 supports at most {maximum} {kind} references per segment")
        request: dict[str, Any] = {
            "prompt": (prompt if prompt_mode == "preserve_tags_only" else prompt.strip()) if isinstance(prompt, str) else "",
            "parameters": dict(parameters),
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_digest": profile.digest(),
            "references": normalized_refs,
        }
        if "director_mode" in value:
            request["director_mode"] = value["director_mode"]
        if "source_asset_id" in value:
            source_asset_id = validate_id(value["source_asset_id"], "source asset id")
            if source_range is not None:
                raise ApiError(
                    400, "source_asset_conflict",
                    "source_range materializes its own source_asset_id; do not provide another source",
                )
            request["source_asset_id"] = source_asset_id
        if prompt_mode != "default":
            request["prompt_mode"] = prompt_mode
        if prompt_mode == "default" and isinstance(parts, dict):
            request["parts"] = dict(parts)
        if pixel_continuation or source_range is not None:
            values: list[str] = [request["prompt"]]
            if isinstance(parts, dict):
                stack: list[Any] = [parts]
                while stack:
                    candidate = stack.pop()
                    if isinstance(candidate, dict):
                        stack.extend(candidate.values())
                    elif isinstance(candidate, list):
                        stack.extend(candidate)
                    elif isinstance(candidate, str):
                        values.append(candidate)
            if any(LITERAL_REFERENCE_TAG.search(text) for text in values):
                raise ApiError(
                    400, "unstable_reference_tag",
                    "derived-reference prompts cannot contain literal <Picture N>, <Video N>, or <Audio N> tags; use a stable @{asset-id} alias",
                )
            explicit_ids = {str(ref["asset_id"]) for ref in normalized_refs}
            unknown_aliases = {
                match.group(1)
                for text in values
                for match in STABLE_REFERENCE_ALIAS.finditer(text)
                if match.group(1) not in explicit_ids
            }
            if unknown_aliases:
                raise ApiError(
                    400, "unknown_reference",
                    "derived-reference prompts may only mention stable aliases for explicitly connected assets",
                )
        if source_range is not None and profile.compiler != "h3_ref":
            raise ApiError(400, "source_range_profile", "source_range requires a Ref2VA profile")
        if continuation == "tail_frame":
            if profile.compiler != "h3_fl":
                raise ApiError(400, "continuation_profile", "tail_frame requires an FL2VA profile")
            roles = {str(ref.get("role")) for ref in normalized_refs}
            if roles - {"last_frame"}:
                raise ApiError(400, "mixed_reference_modes", "tail_frame cannot be mixed with Ref2VA roles or another first frame")
        elif continuation == "previous_video":
            if profile.compiler != "h3_ref":
                raise ApiError(400, "continuation_profile", "previous_video requires a Ref2VA profile")
            if any(str(ref.get("role")) in {"first_frame", "last_frame"} for ref in normalized_refs):
                raise ApiError(400, "mixed_reference_modes", "previous_video cannot be mixed with FL first/last frames")
        elif continuation == "motion_context":
            if profile.compiler not in {"h3_fl", "h3_ref"}:
                raise ApiError(400, "continuation_profile", "motion_context requires an H3 video profile")
            if any(str(ref.get("role")) == "first_frame" for ref in normalized_refs):
                raise ApiError(
                    400, "motion_context_first_frame_conflict",
                    "motion_context owns the opening frames and cannot be combined with a first_frame reference",
                )

        # Preflight the exact typed request, including every slot that will be
        # materialized locally before submit. This prevents a later segment
        # from spending preceding GPU work before its contract error appears.
        occupied = {str(ref["asset_id"]) for ref in normalized_refs}
        placeholders = iter(
            character * 32
            for character in "0123456789abcdef"
            if character * 32 not in occupied
        )
        preflight = request
        synthetic: dict[str, dict[str, Any]] = {}
        if source_range is not None:
            source_id = next(placeholders)
            occupied.add(source_id)
            preflight = self._with_source_range_reference(
                preflight, source_id, include_audio=include_source_audio,
            )
            synthetic[source_id] = {
                "id": source_id,
                "filename": "derived-source-range.mp4",
                "comfy_path": "h3-studio/derived-source-range.mp4",
                "kind": "video",
                "media": {
                    # H3 generation accepts 362 frames, while an individual
                    # reference remains capped at 15 seconds (360 frames).
                    "duration": min(
                        15.0,
                        float(parameters.get("duration", 5)) if pad_source_reference else
                        (source_range["end_frame"] - source_range["start_frame"]) / source_range["fps"],
                    ),
                    "fps": 24.0, "reference_fps": 24.0,
                    "has_video": True, "has_audio": include_source_audio,
                },
            }
        if pixel_continuation:
            continuation_id = next(placeholders)
            preflight = self._with_continuation_reference(preflight, continuation, continuation_id)
            synthetic[continuation_id] = {
                "id": continuation_id,
                "filename": "derived-continuation.png" if continuation == "tail_frame" else "derived-continuation.mp4",
                "comfy_path": "h3-studio/derived-continuation.png" if continuation == "tail_frame" else "h3-studio/derived-continuation.mp4",
                "kind": "image" if continuation == "tail_frame" else "video",
                "media": {} if continuation == "tail_frame" else {
                    "duration": (
                        (continuation_range["end_frame"] - continuation_range["start_frame"])
                        / continuation_range["fps"]
                        if continuation_range is not None else 5.0
                    ),
                    "fps": 24.0, "reference_fps": 24.0,
                    "has_video": True, "has_audio": True,
                },
            }

        def lookup(asset_id: str) -> dict[str, Any]:
            return synthetic.get(asset_id) or self.assets.get(asset_id)

        parse_generation_request({**preflight, "output_type": "video"}, lookup, self.registry)
        return request

    @staticmethod
    def _validate_duration(parameters: dict[str, Any]) -> None:
        try:
            duration = float(parameters.get("duration", 5))
        except (TypeError, ValueError) as error:
            raise ApiError(400, "invalid_parameter", "duration must be a number") from error
        if not 5 <= duration <= H3_MAX_DURATION_SECONDS:
            raise ApiError(
                400,
                "invalid_parameter",
                f"each segment duration must be between 5 and {H3_MAX_DURATION_SECONDS:g} seconds",
            )

    def receipt(self, project: dict[str, Any]) -> dict[str, Any]:
        result = {
            "id": project["id"],
            "title": project.get("title", ""),
            "status": project.get("status", "draft"),
            "current_index": int(project.get("current_index", -1)),
            "selected_segment_ids": list(project.get("selected_segment_ids", [])),
            "stop_requested": bool(project.get("stop_requested", False)),
            "created_at": project.get("created_at"),
            "updated_at": project.get("updated_at"),
            "segments": [],
        }
        if isinstance(project.get("storyboard"), dict):
            result["storyboard"] = dict(project["storyboard"])
        if isinstance(project.get("recipe"), dict):
            result["recipe"] = json.loads(json.dumps(project["recipe"]))
        for segment in project.get("segments", []):
            if segment.get("kind") == "media":
                media_source = dict(segment.get("media_source", {}))
                public = {
                    "id": segment["id"], "index": segment["index"],
                    "kind": "media", "continuation": "none",
                    "status": "completed", "attempts": [],
                    "media_source": media_source,
                }
                if media_source.get("type") == "asset" and media_source.get("asset_id"):
                    asset_id = str(media_source["asset_id"])
                    public.update({
                        "preview_url": f"/api/assets/{asset_id}/content",
                        "download_url": f"/api/assets/{asset_id}/content",
                        "thumbnail_url": f"/api/assets/{asset_id}/thumbnail",
                    })
                elif media_source.get("type") == "job" and media_source.get("job_id"):
                    job_id = str(media_source["job_id"])
                    output_index = int(media_source.get("index", 0) or 0)
                    public.update({
                        "preview_url": f"/api/preview?id={job_id}&index={output_index}",
                        "download_url": f"/api/download?id={job_id}&index={output_index}",
                        "thumbnail_url": f"/api/jobs/{job_id}/thumbnail?index={output_index}",
                    })
                result["segments"].append(public)
                continue
            public = {
                "id": segment["id"], "index": segment["index"],
                "continuation": segment["continuation"], "status": segment["status"],
                "request": segment["request"], "attempts": segment.get("attempts", []),
            }
            if isinstance(segment.get("source_range"), dict):
                public["source_range"] = dict(segment["source_range"])
            if isinstance(segment.get("continuation_range"), dict):
                public["continuation_range"] = dict(segment["continuation_range"])
            if isinstance(segment.get("motion_context"), dict):
                public["motion_context"] = dict(segment["motion_context"])
            for key in ("job_id", "error"):
                if segment.get(key) is not None:
                    public[key] = segment[key]
            if segment.get("status") == "completed" and segment.get("job_id"):
                public["preview_url"] = f"/api/preview?id={segment['job_id']}&index=0"
                public["download_url"] = f"/api/download?id={segment['job_id']}&index=0"
                public["thumbnail_url"] = f"/api/jobs/{segment['job_id']}/thumbnail?index=0"
            result["segments"].append(public)
        if isinstance(project.get("merged"), dict):
            result["merged"] = self._public_merged(project)
        return result

    def _public_merged(self, project: dict[str, Any]) -> dict[str, Any]:
        merged = project.get("merged", {})
        public = {key: merged[key] for key in ("status", "progress", "sha256", "size", "media", "error", "result_job_id", "sources") if key in merged}
        if merged.get("status") == "completed":
            project_id = project["id"]
            public.update({
                "preview_url": f"/api/video-projects/{project_id}/merged/preview",
                "download_url": f"/api/video-projects/{project_id}/merged/download",
                "thumbnail_url": f"/api/video-projects/{project_id}/merged/thumbnail",
            })
        return public

    # ---- workers -------------------------------------------------------

    def _start(self, project_id: str, target: Callable[..., None], *args: Any) -> None:
        worker = threading.Thread(target=target, args=(project_id, *args), name=f"h3-project-{project_id[:8]}", daemon=True)
        self._workers[project_id] = worker
        worker.start()

    def _run_project(self, project_id: str, start_index: int = 0) -> None:
        try:
            while True:
                with self.lock:
                    project = self.store.get(project_id)
                    if project.get("stop_requested"):
                        self._cancel_current_job(project)
                        self._finish_stopped(project)
                        return
                    segments = project.get("segments", [])
                    persisted_selection = project.get("selected_segment_ids")
                    # Legacy running projects have no selection and resume all
                    # unfinished clips. New runs persist an exact allow-list.
                    selected = (
                        {item for item in persisted_selection if isinstance(item, str)}
                        if isinstance(persisted_selection, list) and persisted_selection
                        else {str(segment.get("id")) for segment in segments}
                    )
                    index = next((
                        i for i in range(start_index, len(segments))
                        if str(segments[i].get("id")) in selected
                        and segments[i].get("status") != "completed"
                    ), -1)
                    if index < 0:
                        all_complete = bool(segments) and all(
                            segment.get("status") == "completed" for segment in segments
                        )
                        project.update({
                            "status": "completed" if all_complete else "partial",
                            "current_index": -1, "updated_at": time.time(),
                        })
                        self.store.put(project_id, project)
                        return
                    segment = segments[index]
                    project.update({"status": "running", "current_index": index, "updated_at": time.time()})
                    self.store.put(project_id, project)
                ok = self._execute_segment(project_id, index)
                if not ok:
                    return
                start_index = index + 1
        except Exception as error:  # defensive boundary for daemon worker
            with self.lock:
                try:
                    project = self.store.get(project_id)
                    project.update({"status": "failed", "updated_at": time.time()})
                    current = int(project.get("current_index", -1))
                    if 0 <= current < len(project.get("segments", [])):
                        project["segments"][current].update({"status": "failed", "error": str(error)})
                    self.store.put(project_id, project)
                except Exception:
                    pass

    def _run_single_segment(self, project_id: str, index: int) -> None:
        """Run exactly one explicit target and never spend on later clips."""
        try:
            ok = self._execute_segment(project_id, index)
            if not ok:
                return
            with self.lock:
                project = self.store.get(project_id)
                all_complete = bool(project.get("segments")) and all(
                    segment.get("status") == "completed" for segment in project["segments"]
                )
                project.update({
                    "status": "completed" if all_complete else "partial",
                    "current_index": -1,
                    "updated_at": time.time(),
                })
                self.store.put(project_id, project)
        except Exception as error:
            with self.lock:
                project = self.store.get(project_id)
                project.update({"status": "failed", "current_index": -1, "updated_at": time.time()})
                if 0 <= index < len(project.get("segments", [])):
                    project["segments"][index].update({"status": "failed", "error": str(error)})
                self.store.put(project_id, project)

    def _execute_segment(self, project_id: str, index: int) -> bool:
        with self.lock:
            project = self.store.get(project_id)
            segment = project["segments"][index]
            attempt = next((a for a in reversed(segment.get("attempts", [])) if a.get("status") in {"preparing", "submitting", "queued", "running"}), None)
            job_id = str(attempt.get("job_id", "")) if attempt else ""
            if attempt and not job_id:
                # Close the crash window between durable JobStore creation and
                # attaching its id to the project attempt. A matching job is
                # resumed (and may itself be in `submitting` reconciliation),
                # never submitted a second time.
                recovered = next(
                    (
                        job for job in self.jobs.list()
                        if job.get("video_project_id") == project_id
                        and job.get("segment_id") == segment.get("id")
                        and job.get("attempt_id") == attempt.get("id")
                    ),
                    None,
                )
                if recovered:
                    job_id = str(recovered["id"])
                    attempt.update({"job_id": job_id, "status": str(recovered.get("status", "submitting"))})
            if not attempt:
                attempt = {"id": uuid.uuid4().hex, "status": "preparing", "started_at": time.time(), "continuation": {"mode": segment["continuation"]}}
                segment.setdefault("attempts", []).append(attempt)
            segment.update({"status": "running", "error": None})
            if job_id:
                segment["job_id"] = job_id
            self.store.put(project_id, project)
        if not job_id:
            try:
                request, evidence = self._prepare_request(project, index)
                motion_execution = self._motion_execution(project, index)
                with self.lock:
                    durable = self.store.get(project_id)
                    if durable.get("stop_requested"):
                        self._finish_stopped(durable)
                        return False
                    durable_segment = durable["segments"][index]
                    durable_attempt = next(a for a in durable_segment["attempts"] if a["id"] == attempt["id"])
                    durable_attempt["continuation"] = evidence
                    self.store.put(project_id, durable)
                job_id = self._submit_segment(
                    request, project_id, str(segment["id"]), str(attempt["id"]),
                    motion_execution=motion_execution,
                )
                submitted_job = self.jobs.get(job_id)
                with self.lock:
                    project = self.store.get(project_id)
                    segment = project["segments"][index]
                    active = next(a for a in segment["attempts"] if a["id"] == attempt["id"])
                    active.update({
                        "status": "queued", "job_id": job_id, "continuation": evidence,
                        "workflow_evidence": submitted_job.get("workflow_evidence"),
                    })
                    segment.update({"job_id": job_id, "status": "running"})
                    self.store.put(project_id, project)
            except GenerationStopped as stopped:
                with self.lock:
                    project = self.store.get(project_id)
                    segment = project["segments"][index]
                    active = next(a for a in segment["attempts"] if a["id"] == attempt["id"])
                    active["job_id"] = stopped.job_id
                    segment["job_id"] = stopped.job_id
                    self.store.put(project_id, project)
                self._finish_stopped(project)
                return False
            except Exception as error:
                self._fail_attempt(project_id, index, str(attempt["id"]), error)
                # Preparation/submission failures have no live remote prompt.
                # Reclaim their private crops immediately so repeatedly
                # retrying a bad source cannot exhaust the asset quota.
                try:
                    with self.lock:
                        failed = self.store.get(project_id)
                        self._reclaim_segment_assets(failed, [index])
                        self.store.put(project_id, failed)
                        self._reclaim_orphaned_derived_assets(owner_project_id=project_id)
                except Exception:
                    # Cleanup is best effort and must not replace the durable
                    # generation error; startup reconciliation retries it.
                    pass
                return False
        while True:
            with self.lock:
                project = self.store.get(project_id)
                stop_requested = bool(project.get("stop_requested"))
            if stop_requested:
                try:
                    self._cancel_job(job_id)
                except Exception as error:
                    with self.lock:
                        project = self.store.get(project_id)
                        project.update({
                            "status": "stopping", "stop_requested": True,
                            "stop_warning": str(error) or "ComfyUI cancellation could not be confirmed",
                            "updated_at": time.time(),
                        })
                        self.store.put(project_id, project)
                    time.sleep(2)
                    continue
                self._finish_stopped(project)
                return False
            try:
                job = self._poll_job(job_id)
            except Exception as error:
                self._fail_attempt(project_id, index, str(attempt["id"]), error)
                return False
            state = str(job.get("status"))
            with self.lock:
                project = self.store.get(project_id)
                segment = project["segments"][index]
                active = next(a for a in segment["attempts"] if a["id"] == attempt["id"])
                active["status"] = state
                if state in TERMINAL_JOBS:
                    active["finished_at"] = time.time()
                    segment["status"] = "completed" if state == "completed" else "stopped" if state == "canceled" else "failed"
                    if state != "completed":
                        segment["error"] = str(job.get("message", "generation failed"))
                        active["error"] = segment["error"]
                        project["status"] = "stopped" if state == "canceled" else "failed"
                    if isinstance(job.get("motion_context_state"), dict):
                        active["motion_context_state"] = dict(job["motion_context_state"])
                    if (
                        isinstance(project.get("recipe"), dict)
                        and project["recipe"].get("type") in {
                            CHARACTER_MIGRATION_RECIPE_TYPE, REPLICATION_RECIPE_TYPE,
                        }
                    ):
                        # The immutable source range and completed job output
                        # are sufficient for restart/rerun. The private padded
                        # model input is no longer needed once the job is
                        # terminal, so long projects keep bounded disk usage.
                        self._reclaim_segment_assets(project, [index])
                    project["updated_at"] = time.time()
                    self.store.put(project_id, project)
                    return state == "completed"
                self.store.put(project_id, project)
            time.sleep(POLL_SECONDS)

    def _prepare_request(
        self, project: dict[str, Any], index: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        segment = project["segments"][index]
        self._validate_recipe_assets(project)
        request = json.loads(json.dumps(segment["request"]))
        # Long-video execution always follows the same read-only prompt policy
        # as single-video generation, including legacy persisted projects.
        request["prompt_mode"] = "preserve_tags_only"
        request["parts"] = {}
        mode = segment["continuation"]
        evidence: dict[str, Any] = {"mode": mode}
        if isinstance(segment.get("source_range"), dict):
            request, source_evidence = self._prepare_source_range_reference(
                project, segment, request,
            )
            evidence["source_range"] = source_evidence
        if mode == "none":
            parse_generation_request({**request, "output_type": "video"}, self.assets.get, self.registry)
            return request, evidence
        if index <= 0:
            raise ApiError(400, "invalid_continuation", "continuation requires a previous segment")
        source_segment = project["segments"][index - 1]
        if source_segment.get("status") != "completed" or not source_segment.get("job_id"):
            raise ApiError(409, "continuation_not_ready", "the previous segment has no completed output")
        source_job = self.jobs.get(str(source_segment["job_id"]))
        source_path, source_output = self._job_output(source_job)
        source_sha = self._sha256(source_path)
        recorded_sha = source_output.get("sha256")
        if not isinstance(recorded_sha, str) or re.fullmatch(r"[0-9a-f]{64}", recorded_sha) is None:
            raise ApiError(409, "output_integrity", "the previous segment output has no valid recorded hash")
        if recorded_sha != source_sha:
            raise ApiError(409, "output_integrity", "the previous segment output no longer matches its recorded hash")
        evidence.update({
            "source_segment_id": source_segment["id"],
            "source_job_id": source_job["id"],
            "source_sha256": source_sha,
        })
        if mode == "motion_context":
            current_spec = parse_generation_request(
                {**request, "output_type": "video"}, self.assets.get, self.registry,
            )
            source_parameters = source_job.get("parameters") if isinstance(source_job.get("parameters"), dict) else {}
            source_width = int(source_parameters.get("width", 0) or 0)
            source_height = int(source_parameters.get("height", 0) or 0)
            if (source_width, source_height) != (current_spec.width, current_spec.height):
                raise ApiError(
                    409, "motion_context_resolution_mismatch",
                    f"Motion Context requires matching adjacent resolutions; previous is {source_width}x{source_height}, current is {current_spec.width}x{current_spec.height}",
                )
            manifest = self.motion_contexts.get(str(source_job["id"]))
            settings = segment.get("motion_context") if isinstance(segment.get("motion_context"), dict) else {}
            video_frames = int(settings.get("video_frames", 22))
            audio_frames = int(settings.get("audio_frames", 24))
            evidence.update({
                "video_frames": video_frames,
                "audio_frames": audio_frames,
                "context_sha256": manifest.get("sha256"),
                "context_size": manifest.get("size"),
                "lossless_latent": True,
                "trim": "head_video_and_audio",
                "match_tail": True,
            })
            return request, evidence
        temp = self.config.data_root / "tmp" / f"continuation-{uuid.uuid4().hex}"
        if mode == "tail_frame":
            temp = temp.with_suffix(".png")
            media = source_output.get("media") if isinstance(source_output.get("media"), dict) else {}
            frame_count = int(media.get("frame_count", 0) or 0)
            fps = float(media.get("fps", 0) or 0)
            video_duration = float(media.get("video_duration", 0) or 0)
            if video_duration <= 0 and frame_count > 0 and fps > 0:
                video_duration = frame_count / fps
            if video_duration <= 0:
                video_duration = float(media.get("duration", 0) or 0)
            # Seek relative to the video stream duration, not container EOF:
            # audio may legitimately outlast the last video frame.
            seek = max(0.0, video_duration - 1.0)
            command = [
                "ffmpeg", "-y", "-v", "error", "-ss", f"{seek:.6f}", "-i", str(source_path),
                # `-fps_mode` is unavailable on the FFmpeg shipped by common
                # AutoDL images. `-update 1` is sufficient for a single image
                # muxer target and works across those older releases.
                "-map", "0:v:0", "-an", "-update", "1", str(temp),
            ]
            self._run_command(command, temp, "tail frame extraction")
            asset = self._import_continuation_asset(
                temp, original_filename=f"{source_segment['id']}-tail.png",
                requested_kind="image", claimed_content_type="image/png",
                project_id=str(project["id"]), segment_id=str(segment["id"]), source_sha256=source_sha,
            )
        else:
            temp = temp.with_suffix(".mp4")
            media = source_output.get("media") if isinstance(source_output.get("media"), dict) else {}
            frame_count = int(media.get("frame_count", 0) or 0)
            fps = float(media.get("fps", 0) or 0)
            video_duration = float(media.get("video_duration", 0) or 0)
            if video_duration <= 0 and frame_count > 0 and fps > 0:
                video_duration = frame_count / fps
            container_duration = float(media.get("duration", 0) or 0)
            if video_duration <= 0:
                video_duration = container_duration
            reference_duration = max(video_duration, container_duration)
            continuation_range = segment.get("continuation_range")
            derived_metadata: dict[str, Any] | None = None
            requested_interval: dict[str, Any] | None = None
            original_filename = f"{source_segment['id']}-previous.mp4"
            if isinstance(continuation_range, dict):
                try:
                    source_fps = float(media.get("fps", 0) or 0)
                except (TypeError, ValueError):
                    source_fps = 0.0
                if (
                    not math.isfinite(source_fps)
                    or not math.isclose(source_fps, 24.0, rel_tol=0, abs_tol=0.01)
                ):
                    raise ApiError(
                        409, "continuation_range_source_fps",
                        "the previous segment output is not a verified 24 fps video",
                    )
                source_frame_count = media.get("frame_count")
                if (
                    isinstance(source_frame_count, bool)
                    or not isinstance(source_frame_count, int)
                    or source_frame_count <= 0
                ):
                    source_frame_count = (
                        int(math.floor(video_duration * source_fps + 1e-6))
                        if math.isfinite(video_duration) and video_duration > 0 else 0
                    )
                start_frame = int(continuation_range["start_frame"])
                end_frame = int(continuation_range["end_frame"])
                selected_frame_count = end_frame - start_frame
                if source_frame_count <= 0 or end_frame > source_frame_count:
                    raise ApiError(
                        409, "continuation_range_out_of_bounds",
                        "continuation_range exceeds the actual previous segment output",
                    )
                selected_duration = selected_frame_count / 24.0
                command = [
                    "ffmpeg", "-y", "-v", "error", "-i", str(source_path),
                    "-map", "0:v:0",
                    "-vf", (
                        f"trim=start_frame={start_frame}:end_frame={end_frame},"
                        "setpts=PTS-STARTPTS"
                    ),
                    "-frames:v", str(selected_frame_count), "-an", "-r", "24",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(temp),
                ]
                self._run_command(command, temp, "previous video continuation range extraction")
                requested_interval = {
                    "start_frame": start_frame, "end_frame": end_frame,
                    "fps": 24.0, "frame_count": selected_frame_count,
                    "start_time": start_frame / 24.0,
                    "end_time": end_frame / 24.0,
                    "duration": selected_duration,
                }
                evidence["reference_transform"] = "video_only_exact_frame_range_reencode"
                evidence["trimmed_for_reference"] = True
                evidence["reference_duration_limit"] = 15.0
                derived_metadata = {
                    "start_frame": start_frame, "end_frame": end_frame,
                    "fps": 24.0, "frame_count": selected_frame_count,
                    "range_kind": "previous_video",
                }
                original_filename = (
                    f"{source_segment['id']}-previous-{start_frame}-{end_frame}.mp4"
                )
            elif reference_duration > 15:
                # H3 may generate the final 362-frame grid point (15.083s),
                # while a Ref2VA video input must still be at most 15s. Trim
                # only the trusted derived continuation copy; user uploads
                # continue to be rejected when they exceed the input budget.
                command = [
                    "ffmpeg", "-y", "-v", "error", "-i", str(source_path),
                    "-t", "15", "-map", "0:v:0", "-an", "-r", "24",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temp),
                ]
                self._run_command(command, temp, "previous video continuation trim")
                evidence["source_duration"] = reference_duration
                evidence["source_video_duration"] = video_duration
                evidence["reference_duration_limit"] = 15.0
                evidence["trimmed_for_reference"] = True
                evidence["reference_transform"] = "video_only_trim_reencode"
            else:
                # Even short H3 outputs can carry an audio stream. A Ref2VA
                # continuation is a motion reference, so materialize a new
                # video-only container instead of copying the source bytes.
                command = [
                    "ffmpeg", "-y", "-v", "error", "-i", str(source_path),
                    "-map", "0:v:0", "-an", "-c:v", "copy",
                    "-movflags", "+faststart", str(temp),
                ]
                try:
                    self._run_command(command, temp, "previous video continuation audio removal")
                    evidence["reference_transform"] = "video_only_stream_copy"
                except ApiError as error:
                    if error.code != "ffmpeg_failed":
                        raise
                    # Some otherwise decodable H3 outputs use a codec/profile
                    # that cannot be copied into MP4. Preserve the video-only
                    # invariant by falling back to a bounded 24fps encode.
                    command = [
                        "ffmpeg", "-y", "-v", "error", "-i", str(source_path),
                        "-map", "0:v:0", "-an", "-r", "24", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temp),
                    ]
                    self._run_command(command, temp, "previous video continuation audio removal fallback")
                    evidence["reference_transform"] = "video_only_reencode"
                evidence["trimmed_for_reference"] = False
            evidence["source_duration"] = reference_duration
            evidence["source_video_duration"] = video_duration
            evidence["audio_policy"] = "video_only"
            evidence["source_had_audio"] = bool(media.get("has_audio", False))
            asset = self._import_continuation_asset(
                temp, original_filename=original_filename,
                requested_kind="video", claimed_content_type="video/mp4",
                project_id=str(project["id"]), segment_id=str(segment["id"]), source_sha256=source_sha,
                derived_metadata=derived_metadata,
            )
            asset_media = asset.get("media") if isinstance(asset.get("media"), dict) else {}
            if asset_media.get("has_audio") is not False:
                self.assets.delete(str(asset["id"]))
                raise ApiError(422, "continuation_audio_present", "previous-video continuation could not be materialized without audio")
            if requested_interval is not None:
                try:
                    effective_fps = float(asset_media.get("fps", 0) or 0)
                    effective_duration = float(
                        asset_media.get("video_duration", 0)
                        or asset_media.get("duration", 0)
                        or 0
                    )
                except (TypeError, ValueError):
                    effective_fps = 0.0
                    effective_duration = 0.0
                effective_frame_count = asset_media.get("frame_count")
                expected_frame_count = int(requested_interval["frame_count"])
                expected_duration = expected_frame_count / 24.0
                duration_tolerance = 1.0 / 24.0 + 1e-3
                derived_matches = (
                    math.isfinite(effective_fps)
                    and math.isclose(effective_fps, 24.0, rel_tol=0, abs_tol=0.01)
                    and not isinstance(effective_frame_count, bool)
                    and isinstance(effective_frame_count, int)
                    and effective_frame_count == expected_frame_count
                    and math.isfinite(effective_duration)
                    and 0 < effective_duration <= 15.0
                    and abs(effective_duration - expected_duration) <= duration_tolerance
                    and abs(effective_duration - effective_frame_count / effective_fps) <= duration_tolerance
                )
                if not derived_matches:
                    self.assets.delete(str(asset["id"]))
                    raise ApiError(
                        422, "continuation_range_output_mismatch",
                        "the derived continuation does not match the requested 24 fps frame interval",
                    )
                effective_interval = {
                    "start_frame": int(requested_interval["start_frame"]),
                    "end_frame": int(requested_interval["start_frame"]) + effective_frame_count,
                    "fps": effective_fps, "frame_count": effective_frame_count,
                    "start_time": float(requested_interval["start_time"]),
                    "end_time": float(requested_interval["start_time"]) + effective_duration,
                    "duration": effective_duration,
                }
                evidence["continuation_range"] = {
                    **effective_interval,
                    "requested": dict(requested_interval),
                    "effective": effective_interval,
                    "source": {
                        "fps": source_fps, "frame_count": source_frame_count,
                        "video_duration": video_duration,
                    },
                }
            evidence["reference_has_audio"] = False
            evidence["reference_video_codec"] = asset_media.get("video_codec")
        evidence.update({
            "asset_id": asset["id"], "asset_sha256": asset.get("sha256"),
            "asset_size": asset.get("storage_size", asset.get("size")),
            "asset_kind": asset.get("kind"),
        })
        request = self._with_continuation_reference(request, mode, str(asset["id"]))
        parse_generation_request({**request, "output_type": "video"}, self.assets.get, self.registry)
        return request, evidence

    def _motion_execution(self, project: dict[str, Any], index: int) -> dict[str, Any]:
        segments = project.get("segments", [])
        execution: dict[str, Any] = {
            "save_context": (
                index + 1 < len(segments)
                and segments[index + 1].get("kind", "generation") == "generation"
                and segments[index + 1].get("continuation") == "motion_context"
            ),
        }
        segment = segments[index]
        if segment.get("continuation") != "motion_context":
            return execution
        source = segments[index - 1]
        source_job_id = validate_id(str(source.get("job_id", "")), "source job id")
        settings = segment.get("motion_context") if isinstance(segment.get("motion_context"), dict) else {}
        execution.update({
            "source_manifest": self.motion_contexts.get(source_job_id),
            "video_frames": int(settings.get("video_frames", 22)),
            "audio_frames": int(settings.get("audio_frames", 24)),
        })
        return execution

    def _prepare_source_range_reference(
        self,
        project: dict[str, Any],
        segment: dict[str, Any],
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Decode an exact source frame interval into a private Ref2VA input."""
        source_range = segment.get("source_range")
        if not isinstance(source_range, dict):
            return request, {}
        source_asset = self.assets.get(str(source_range["asset_id"]))
        if source_asset.get("kind") != "video":
            raise ApiError(409, "source_asset_not_video", "source_range asset is no longer a video")
        original_path = self.assets.content_path(source_asset)
        source_sha = self._sha256(original_path)
        recorded_sha = source_asset.get("sha256")
        if (
            isinstance(recorded_sha, str)
            and re.fullmatch(r"[0-9a-f]{64}", recorded_sha)
            and recorded_sha != source_sha
        ):
            raise ApiError(409, "source_integrity", "source_range asset no longer matches its recorded hash")
        recipe = project.get("recipe") if isinstance(project.get("recipe"), dict) else {}
        if (
            recipe.get("type") in {CHARACTER_MIGRATION_RECIPE_TYPE, REPLICATION_RECIPE_TYPE}
            and recipe.get("source_sha256") != source_sha
        ):
            raise ApiError(409, "source_integrity", "source video changed after project planning; create a new plan")
        source_path = original_path
        media = source_asset.get("media") if isinstance(source_asset.get("media"), dict) else {}
        if media.get("normalized_to_24fps"):
            comfy_path = str(source_asset.get("comfy_path", ""))
            relative = comfy_path.removeprefix("h3-studio/")
            source_path = secure_join(self.assets.upload_root, relative)
            if not source_path.is_file():
                raise ApiError(409, "source_normalization_missing", "the source 24fps normalization is missing; re-upload the source")

        start_frame = int(source_range["start_frame"])
        end_frame = int(source_range["end_frame"])
        fps = float(source_range["fps"])
        frame_count = end_frame - start_frame
        duration = frame_count / fps
        temp = self.config.data_root / "tmp" / f"source-range-{uuid.uuid4().hex}.mp4"
        temp.parent.mkdir(parents=True, exist_ok=True)
        is_migration = recipe.get("type") == CHARACTER_MIGRATION_RECIPE_TYPE
        is_planned_replication = recipe.get("type") == REPLICATION_RECIPE_TYPE
        include_audio = (is_migration or is_planned_replication) and recipe.get("audio_policy") == "reference-source"
        segmentation = recipe.get("segmentation") if isinstance(recipe.get("segmentation"), dict) else {}
        configured_frames = int(segmentation.get("segment_frames", frame_count) or frame_count)
        if is_planned_replication:
            configured_frames = round(float(request["parameters"]["duration"]) * 24)
        # H3 references have a hard 15-second/360-frame ceiling. Planned
        # tail windows are padded only in this private model input so source
        # timeline accounting and final trimming remain exact.
        materialized_frames = min(360, configured_frames) if is_migration or is_planned_replication else min(360, frame_count)
        decoded_frames = min(frame_count, materialized_frames)
        pad_frames = max(0, materialized_frames - decoded_frames)
        video_filter = f"trim=start_frame={start_frame}:end_frame={start_frame + decoded_frames},setpts=PTS-STARTPTS"
        if pad_frames:
            video_filter += f",tpad=stop_mode=clone:stop={pad_frames}"
        command = [
            "ffmpeg", "-y", "-v", "error", "-i", str(source_path),
            "-map", "0:v:0",
            "-vf", video_filter,
            "-frames:v", str(materialized_frames),
        ]
        if include_audio:
            start_time = start_frame / fps
            materialized_duration = materialized_frames / fps
            decoded_end_time = (start_frame + decoded_frames) / fps
            command.extend([
                "-map", "0:a:0", "-af",
                (
                    f"atrim=start={start_time:.9f}:end={decoded_end_time:.9f},"
                    f"asetpts=PTS-STARTPTS,aresample=48000,apad=pad_dur={materialized_duration:.9f},"
                    f"atrim=duration={materialized_duration:.9f}"
                ),
                "-c:a", "aac", "-ar", "48000", "-ac", "2",
            ])
        else:
            command.append("-an")
        command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temp)])
        self._run_command(command, temp, "source range extraction")
        asset = self._import_continuation_asset(
            temp,
            original_filename=f"{segment['id']}-source-{start_frame}-{end_frame}.mp4",
            requested_kind="video", claimed_content_type="video/mp4",
            project_id=str(project["id"]), segment_id=str(segment["id"]),
            source_sha256=source_sha, derived_kind="source_range",
            derived_metadata={
                "source_asset_id": str(source_asset["id"]),
                "start_frame": start_frame, "end_frame": end_frame, "fps": fps,
            },
        )
        prepared = self._with_source_range_reference(
            request, str(asset["id"]), include_audio=include_audio,
            source_subject=(
                str(recipe.get("targets", [{}])[0].get("source_subject", ""))
                if recipe.get("type") == CHARACTER_MIGRATION_RECIPE_TYPE else ""
            ),
        )
        return prepared, {
            "source_asset_id": str(source_asset["id"]),
            "source_sha256": source_sha,
            "start_frame": start_frame, "end_frame": end_frame,
            "fps": fps, "frame_count": frame_count, "duration": duration,
            "materialized_frame_count": materialized_frames,
            "input_padding_frames": pad_frames,
            "asset_id": str(asset["id"]), "asset_sha256": asset.get("sha256"),
            "asset_size": asset.get("storage_size", asset.get("size")),
            "asset_kind": asset.get("kind"),
            "include_audio": include_audio,
        }

    def _director_source_or_generic_r2v(
        self, result: dict[str, Any], asset_id: str, known_video_ids: set[str],
    ) -> None:
        """Use Director source modes only when there is exactly one video.

        Historical long-video continuation may legitimately compose multiple
        video references.  That remains generic R2V and must not be
        misrepresented as the stricter Director RV2V preset.
        """

        video_count = 0
        for reference in result.get("references", []):
            reference_id = str(reference.get("asset_id", reference.get("id", "")))
            if reference_id in known_video_ids:
                video_count += 1
                continue
            try:
                if self.assets.get(reference_id).get("kind") == "video":
                    video_count += 1
            except ApiError:
                # Synthetic preflight assets are represented in
                # known_video_ids; any other missing asset is rejected by the
                # final typed request lookup.
                continue
        if video_count > 1:
            result.pop("source_asset_id", None)
            result["director_mode"] = "r2v"
        else:
            result["source_asset_id"] = asset_id
            result["director_mode"] = "v2v" if len(result.get("references", [])) == 1 else "rv2v"

    def _with_source_range_reference(
        self,
        request: dict[str, Any],
        asset_id: str,
        *,
        include_audio: bool = False,
        source_subject: str = "",
    ) -> dict[str, Any]:
        result = json.loads(json.dumps(request))
        result["references"] = [
            *result.get("references", []),
            {"asset_id": asset_id, "role": "motion", "include_audio": include_audio},
        ]
        if include_audio:
            # Director V2V currently rejects source audio. Generic Ref2VA is
            # the supported compiler path for a motion video whose audio must
            # condition generation; it retains Video 1 / Picture 1 ordering.
            result.pop("source_asset_id", None)
            result["director_mode"] = "r2v"
        else:
            self._director_source_or_generic_r2v(result, asset_id, {asset_id})
        source_instruction = (
            f"Bind <Subject 1> from @{{{asset_id}}} to {source_subject}."
            if source_subject else f"@{{{asset_id}}}"
        )
        result["prompt"] = "; ".join(
            part for part in (
                str(result.get("prompt", "")).strip(),
                source_instruction,
            ) if part
        )
        return result

    def _validate_recipe_assets(self, project: dict[str, Any]) -> None:
        recipe = project.get("recipe")
        if not isinstance(recipe, dict) or recipe.get("type") not in {
            CHARACTER_MIGRATION_RECIPE_TYPE, REPLICATION_RECIPE_TYPE,
        }:
            return
        source = self.assets.get(str(recipe.get("source_asset_id", "")))
        if source.get("sha256") != recipe.get("source_sha256"):
            raise ApiError(409, "source_integrity", "source video changed after project planning; create a new plan")
        if recipe.get("type") == REPLICATION_RECIPE_TYPE:
            for reference in recipe.get("references", []):
                asset = self.assets.get(str(reference.get("asset_id", "")))
                if asset.get("sha256") != reference.get("sha256"):
                    raise ApiError(409, "reference_integrity", "a replication reference changed after planning; create a new plan")
            return
        targets = recipe.get("targets")
        target = targets[0] if isinstance(targets, list) and targets and isinstance(targets[0], dict) else {}
        character = self.assets.get(str(target.get("character_asset_id", "")))
        if character.get("sha256") != target.get("character_sha256"):
            raise ApiError(409, "character_integrity", "target character changed after character-migration planning; create a new plan")

    def _with_continuation_reference(self, request: dict[str, Any], mode: str, asset_id: str) -> dict[str, Any]:
        result = json.loads(json.dumps(request))
        stable = f"@{{{asset_id}}}"
        if mode == "tail_frame":
            result["references"] = [
                {"asset_id": asset_id, "role": "first_frame"},
                *result.get("references", []),
            ]
            result.pop("source_asset_id", None)
            result["director_mode"] = "i2v" if len(result["references"]) == 1 else "fl2v"
            instruction = (
                stable
                if result.get("prompt_mode") == "preserve_tags_only"
                else (
                    f"At 0.00 seconds, continue seamlessly from {stable}; preserve identity, wardrobe, "
                    "color, key objects, composition, lighting, scene geography, spatial relationships, and screen direction."
                )
            )
        elif mode == "previous_video":
            prior_source_id = str(result.get("source_asset_id", ""))
            result["references"] = [
                *result.get("references", []),
                {"asset_id": asset_id, "role": "motion", "include_audio": False},
            ]
            known_videos = {asset_id}
            if prior_source_id:
                known_videos.add(prior_source_id)
            self._director_source_or_generic_r2v(result, asset_id, known_videos)
            instruction = (
                stable
                if result.get("prompt_mode") == "preserve_tags_only"
                else (
                    f"Continue the preceding action, motion phase, camera trajectory, scene geography, and screen direction from {stable}; "
                    "preserve the target identity and do not copy the source identity or reuse its audio."
                )
            )
        else:
            return result
        result["prompt"] = "; ".join(
            part for part in (str(result.get("prompt", "")).strip(), instruction) if part
        )
        return result

    def _submit_segment(
        self,
        request: dict[str, Any],
        project_id: str,
        segment_id: str,
        attempt_id: str,
        *,
        motion_execution: dict[str, Any] | None = None,
    ) -> str:
        motion_execution = motion_execution or {}
        with self.lock:
            active = [job for job in self.jobs.list() if job.get("status") in ACTIVE_JOBS]
            if len(active) >= self.config.max_active_jobs:
                raise ApiError(429, "job_limit", f"at most {self.config.max_active_jobs} active jobs are allowed")
            spec = parse_generation_request({**request, "output_type": "video"}, self.assets.get, self.registry)
            job_id = uuid.uuid4().hex
            now = time.time()
            job = {
                "id": job_id, "job_id": job_id, "request_id": uuid.uuid4().hex,
                "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "prompt_id": None, "client_id": f"h3-project-{project_id}-{segment_id}-{attempt_id}",
                "status": "submitting", "output_type": "video", "raw_prompt": request.get("prompt", ""),
                "prompt_parts": request.get("parts", {}), "prompt": spec.prompt, "negative_prompt": spec.negative_prompt,
                "parameters": spec.public_parameters(),
                "director_mode": spec.director_mode or None,
                "source_asset_id": spec.source_asset_id or None,
                "references": [
                    {"asset_id": ref.asset_id, "kind": ref.kind, "role": ref.role, "tag_label": ref.label,
                     "include_audio": ref.include_audio, "duration": ref.duration,
                     "voice_speaker": ref.voice_speaker, "voice_subject": ref.voice_subject}
                    for ref in spec.references
                ],
                "video_project_id": project_id, "segment_id": segment_id, "attempt_id": attempt_id,
                "motion_context_save_pending": bool(motion_execution.get("save_context")),
                "created_at": now, "updated_at": now, "submission_started_at": now,
            }
            self.jobs.put(job_id, job)
        staged_path: Path | None = None
        try:
            self.comfy.ensure_capability(spec, self.config, self.registry)
            motion_plan: MotionContextPlan | None = None
            source_manifest = motion_execution.get("source_manifest")
            if source_manifest is not None or motion_execution.get("save_context"):
                ensure_motion_context = getattr(self.comfy, "ensure_motion_context", None)
                if not callable(ensure_motion_context):
                    raise ApiError(503, "motion_context_unavailable", "ComfyUI client cannot verify Motion Context support")
                ensure_motion_context(self.config, self.registry)
                checkpoint_input = ""
                if isinstance(source_manifest, dict):
                    checkpoint_input, staged_path = self.motion_contexts.stage(source_manifest, job_id)
                    job["motion_context_staged_file"] = staged_path.name
                motion_plan = MotionContextPlan(
                    checkpoint_input=checkpoint_input,
                    save_context=bool(motion_execution.get("save_context")),
                    video_frames=int(motion_execution.get("video_frames", 22)),
                    audio_frames=int(motion_execution.get("audio_frames", 24)),
                )
                job["motion_context"] = {
                    "enabled": bool(checkpoint_input),
                    "save_pending": motion_plan.save_context,
                    "video_frames": motion_plan.video_frames,
                    "audio_frames": motion_plan.audio_frames,
                    **({
                        "source_job_id": source_manifest.get("job_id"),
                        "source_sha256": source_manifest.get("sha256"),
                    } if isinstance(source_manifest, dict) else {}),
                }
            workflow = compile_workflow(
                spec, self.config, job_id, motion_context_plan=motion_plan,
            )
            encoded = json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            job["workflow_sha256"] = hashlib.sha256(encoded).hexdigest()
            job["workflow_evidence"] = {
                **workflow_evidence(workflow, spec, job_id),
                "sha256": job["workflow_sha256"],
            }
            evidence_dir = self.config.data_root / "evidence" / "workflows"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence_path = evidence_dir / f"{job_id}.json"
            staging = evidence_path.with_suffix(f".tmp-{uuid.uuid4().hex}")
            staging.write_bytes(encoded)
            staging.replace(evidence_path)
            self.jobs.put(job_id, job)
            # Serialize the final stop check with stop(). If stop was accepted
            # first, no paid prompt is submitted; if submit started first,
            # stop waits and then cancels the now-durable prompt.
            with self.lock:
                project = self.store.get(project_id)
                if project.get("stop_requested"):
                    job.update({
                        "status": "canceled", "message": "canceled before generation submission",
                        "updated_at": time.time(),
                    })
                    self.jobs.put(job_id, job)
                    raise GenerationStopped(job_id)
                if self.comfy_tasks is not None:
                    job = self.comfy_tasks.schedule(job_id, workflow)
                    prompt_id = job.get("prompt_id")
                else:
                    prompt_id = self.comfy.submit(workflow, str(job["client_id"]))
        except GenerationStopped:
            self.motion_contexts.cleanup_staged(staged_path)
            raise
        except Exception as error:
            self.motion_contexts.cleanup_staged(staged_path)
            job.update({"status": "failed", "message": error.message if isinstance(error, ApiError) else "generation submission failed", "updated_at": time.time()})
            self.jobs.put(job_id, job)
            raise
        if isinstance(prompt_id, str):
            job.update({"prompt_id": prompt_id, "status": "queued", "updated_at": time.time()})
            self.jobs.put(job_id, job)
        return job_id

    def _poll_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job.get("status") in TERMINAL_JOBS:
            if self._cleanup_motion_staging(job):
                self.jobs.put(job_id, job)
            return job
        if job.get("status") == "submitting":
            if self.comfy_tasks is not None:
                resource = self.comfy_tasks.queued_status(job)
                if resource and resource.get("status") in {"queued", "running"}:
                    return job
            now = time.time()
            started = float(job.get("submission_started_at", job.get("created_at", 0)) or 0)
            expired = now - started >= self.config.submit_reconcile_grace_seconds
            last_check = float(job.get("submission_reconcile_checked_at", 0) or 0)
            if not expired and now - last_check < SUBMIT_RECONCILE_INTERVAL_SECONDS:
                return job
            try:
                prompt_id = self.comfy.find_prompt_by_client_id(str(job.get("client_id", "")))
            except ApiError:
                prompt_id = None
            if not prompt_id:
                job["submission_reconcile_checked_at"] = now
                if not expired:
                    self.jobs.put(job_id, job)
                    return job
                job.update({
                    "status": "failed",
                    "message": "ComfyUI submission could not be verified after the recovery grace period; it was not resubmitted",
                    "updated_at": time.time(),
                })
                self.jobs.put(job_id, job)
                return job
            job.update({"prompt_id": prompt_id, "status": "queued", "updated_at": time.time()})
            self.jobs.put(job_id, job)
        prompt_id = job.get("prompt_id")
        if not isinstance(prompt_id, str):
            return job
        status = self.comfy.status(prompt_id)
        state = str(status.get("status", "failed"))
        if state in {"error", "not_found"}:
            state = "failed"
        record = status.get("record")
        if state == "completed" and isinstance(record, dict):
            outputs = find_outputs(record, "video")
            if not outputs:
                state = "failed"
                status["message"] = "ComfyUI completed without an expected video output"
            else:
                job["outputs"] = [self._enrich_output(output) for output in outputs]
                if job.get("motion_context_save_pending"):
                    try:
                        manifest = self.motion_contexts.capture(
                            job,
                            record,
                            project_id=str(job.get("video_project_id", "")),
                            segment_id=str(job.get("segment_id", "")),
                            attempt_id=str(job.get("attempt_id", "")),
                        )
                        job["motion_context_state"] = {
                            "available": True,
                            "sha256": manifest.get("sha256"),
                            "size": manifest.get("size"),
                        }
                    except Exception as error:
                        # The MP4 is the primary result. Context persistence is
                        # best effort and may only block the following link.
                        job["motion_context_state"] = {
                            "available": False,
                            "error": error.message if isinstance(error, ApiError) else "Motion Context latent could not be stored",
                        }
                self._cleanup_motion_staging(job)
        if self.comfy_tasks is not None and state in TERMINAL_JOBS:
            self.comfy_tasks.notify_terminal(job, state)
        if state in TERMINAL_JOBS:
            self._cleanup_motion_staging(job)
        with self.lock:
            latest = self.jobs.get(job_id)
            if latest.get("status") in TERMINAL_JOBS:
                return latest
            job.update({"status": state, "updated_at": time.time()})
            if status.get("message"):
                job["message"] = str(status["message"])
            self.jobs.put(job_id, job)
        return job

    def _cleanup_motion_staging(self, job: dict[str, Any]) -> bool:
        staged_name = job.pop("motion_context_staged_file", None)
        if not isinstance(staged_name, str):
            return False
        try:
            staged = secure_join(self.motion_contexts.input_root, staged_name)
        except ApiError:
            staged = None
        self.motion_contexts.cleanup_staged(staged)
        return True

    def _import_continuation_asset(
        self,
        temp: Path,
        *,
        original_filename: str,
        requested_kind: str,
        claimed_content_type: str,
        project_id: str,
        segment_id: str,
        source_sha256: str,
        derived_kind: str = "continuation",
        derived_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if self.assets.used_bytes() + temp.stat().st_size > self.config.max_asset_storage_bytes:
                temp.unlink(missing_ok=True)
                raise ApiError(507, "asset_quota", "continuation asset would exceed the asset storage quota")
            asset = self.assets.import_file(
                temp, original_filename=original_filename, requested_kind=requested_kind,
                claimed_content_type=claimed_content_type,
            )
            asset["derived"] = {
                "kind": derived_kind, "project_id": project_id,
                "segment_id": segment_id, "source_sha256": source_sha256,
                **(derived_metadata or {}),
            }
            self.assets.metadata.put(str(asset["id"]), asset)
            if self.assets.used_bytes() > self.config.max_asset_storage_bytes:
                self.assets.delete(str(asset["id"]))
                raise ApiError(507, "asset_quota", "normalized continuation asset would exceed the asset storage quota")
            return asset

    def _cancel_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job.get("status") in TERMINAL_JOBS:
                return
            prompt_id = job.get("prompt_id")
            if self.comfy_tasks is not None:
                self.comfy_tasks.cancel(job)
                if not isinstance(prompt_id, str):
                    job.update({
                        "status": "canceled", "message": "canceled by project stop before ComfyUI submission",
                        "updated_at": time.time(),
                    })
                    self.jobs.put(job_id, job)
                    return
                job.update({
                    "status": "canceled", "message": "canceled by project stop",
                    "updated_at": time.time(),
                })
                self.jobs.put(job_id, job)
                return
        if not isinstance(prompt_id, str) and isinstance(job.get("client_id"), str):
            try:
                prompt_id = self.comfy.find_prompt_by_client_id(str(job["client_id"]))
            except ApiError:
                prompt_id = None
        if not isinstance(prompt_id, str):
            raise ApiError(503, "cancel_unconfirmed", "the active ComfyUI prompt could not be identified for cancellation")
        self.comfy.cancel(prompt_id)
        with self.lock:
            job = self.jobs.get(job_id)
            job.update({
                "prompt_id": job.get("prompt_id") or prompt_id,
                "status": "canceled", "message": "canceled by project stop",
                "updated_at": time.time(),
            })
            self.jobs.put(job_id, job)

    def _cancel_current_job(self, project: dict[str, Any]) -> None:
        current = int(project.get("current_index", -1))
        segments = project.get("segments", [])
        if 0 <= current < len(segments):
            job_id = segments[current].get("job_id")
            if not isinstance(job_id, str):
                attempts = segments[current].get("attempts", [])
                active = next(
                    (attempt for attempt in reversed(attempts) if attempt.get("status") in {"preparing", "submitting", "queued", "running"}),
                    None,
                )
                if active and isinstance(active.get("job_id"), str):
                    job_id = active["job_id"]
                elif active:
                    recovered = next(
                        (
                            job for job in self.jobs.list()
                            if job.get("video_project_id") == project.get("id")
                            and job.get("segment_id") == segments[current].get("id")
                            and job.get("attempt_id") == active.get("id")
                        ),
                        None,
                    )
                    if recovered:
                        job_id = str(recovered["id"])
                        active["job_id"] = job_id
                        segments[current]["job_id"] = job_id
            if isinstance(job_id, str):
                self._cancel_job(job_id)

    def _reconcile_stopping(self, project: dict[str, Any]) -> bool:
        """Cancel the durable prompt before attesting a stopped restart."""
        active_merge = next(
            (attempt for attempt in reversed(project.get("merge_attempts", [])) if attempt.get("status") == "merging"),
            None,
        )
        if active_merge:
            self._cleanup_merge_paths(project)
            active_merge.update({
                "status": "canceled", "error": "server restarted while merge cancellation was pending",
                "finished_at": time.time(),
            })
            project.update({
                "merged": {"status": "canceled", "error": active_merge["error"]},
                "status": "stopped", "current_index": -1,
                "stop_requested": True, "updated_at": time.time(),
            })
            self.store.put(str(project["id"]), project)
            return True
        error: str | None = None
        try:
            self._cancel_current_job(project)
        except Exception as exc:  # terminalize locally but retain truthful evidence
            error = str(exc) or "ComfyUI cancellation could not be confirmed"
        if error:
            # Never attest stopped/canceled while a remote paid prompt may
            # still be active. Keep the durable state retryable and truthful.
            project.update({
                "status": "stopping", "stop_requested": True,
                "stop_warning": error, "updated_at": time.time(),
            })
            self.store.put(str(project["id"]), project)
            return False
        current = int(project.get("current_index", -1))
        segments = project.get("segments", [])
        if 0 <= current < len(segments):
            segment = segments[current]
            if segment.get("status") in {"running", "pending", "stale"}:
                segment["status"] = "stopped"
            attempts = segment.get("attempts", [])
            active = next(
                (attempt for attempt in reversed(attempts) if attempt.get("status") in {"preparing", "submitting", "queued", "running"}),
                None,
            )
            if active:
                active.update({"status": "canceled", "finished_at": time.time()})
        project.update({"status": "stopped", "current_index": -1, "stop_requested": True, "updated_at": time.time()})
        project.pop("stop_warning", None)
        self.store.put(str(project["id"]), project)
        return True

    def _reclaim_segment_assets(
        self,
        project: dict[str, Any],
        indices: Any,
        *,
        deleting: bool = False,
    ) -> int:
        """Reclaim owned derived binaries while retaining attempt hashes."""
        reclaimed = 0
        selected = set(indices)
        for segment in project.get("segments", []):
            if int(segment.get("index", -1)) not in selected:
                continue
            for attempt in segment.get("attempts", []):
                for evidence in self._attempt_derived_evidence(attempt):
                    asset_id = evidence.get("asset_id")
                    if not isinstance(asset_id, str) or evidence.get("asset_reclaimed_at"):
                        continue
                    try:
                        asset = self.assets.get(asset_id)
                    except ApiError as error:
                        if error.status == 404:
                            evidence["asset_reclaimed_at"] = time.time()
                            continue
                        raise
                    derived = asset.get("derived")
                    if not isinstance(derived, dict) or derived.get("project_id") != project.get("id"):
                        continue
                    if self._asset_has_external_live_reference(
                        asset_id, str(project.get("id")), include_owner_requests=not deleting,
                    ):
                        continue
                    self.assets.delete(asset_id)
                    evidence["asset_reclaimed_at"] = time.time()
                    evidence["asset_reclaimed_reason"] = "project deleted" if deleting else "attempt superseded"
                    reclaimed += 1
        return reclaimed

    def _prune_motion_contexts(self, project: dict[str, Any]) -> int:
        """Keep only current predecessor latents that a chain can still consume."""
        segments = project.get("segments", [])
        keep: set[str] = set()
        for index in range(max(0, len(segments) - 1)):
            source = segments[index]
            target = segments[index + 1]
            job_id = source.get("job_id")
            if target.get("continuation") == "motion_context" and isinstance(job_id, str):
                keep.add(job_id)
        return self.motion_contexts.prune_project(str(project["id"]), keep)

    @staticmethod
    def _attempt_derived_evidence(attempt: Any) -> list[dict[str, Any]]:
        if not isinstance(attempt, dict):
            return []
        continuation = attempt.get("continuation")
        if not isinstance(continuation, dict):
            return []
        records = [continuation] if isinstance(continuation.get("asset_id"), str) else []
        source_range = continuation.get("source_range")
        if isinstance(source_range, dict) and isinstance(source_range.get("asset_id"), str):
            records.append(source_range)
        return records

    def _reclaim_orphaned_derived_assets(self, owner_project_id: str | None = None) -> int:
        """Delete derived artifacts that no durable request/attempt/job owns."""
        attached: set[str] = set()
        for project in self.store.list():
            for segment in project.get("segments", []):
                if not isinstance(segment, dict):
                    continue
                request = segment.get("request")
                if isinstance(request, dict):
                    for reference in request.get("references", []):
                        if isinstance(reference, dict) and isinstance(reference.get("asset_id"), str):
                            attached.add(reference["asset_id"])
                for attempt in segment.get("attempts", []):
                    for evidence in self._attempt_derived_evidence(attempt):
                        attached.add(str(evidence["asset_id"]))
        for job in self.jobs.list():
            if job.get("status") not in ACTIVE_JOBS:
                continue
            for reference in job.get("references", []):
                if isinstance(reference, dict) and isinstance(reference.get("asset_id"), str):
                    attached.add(reference["asset_id"])

        reclaimed = 0
        for asset in self.assets.list():
            derived = asset.get("derived")
            if not isinstance(derived, dict) or derived.get("kind") not in {"continuation", "source_range"}:
                continue
            if owner_project_id is not None and derived.get("project_id") != owner_project_id:
                continue
            asset_id = asset.get("id")
            if not isinstance(asset_id, str) or asset_id in attached:
                continue
            self.assets.delete(asset_id)
            reclaimed += 1
        return reclaimed

    def _asset_has_external_live_reference(
        self, asset_id: str, owner_project_id: str, *, include_owner_requests: bool = False,
    ) -> bool:
        for candidate in self.store.list():
            is_owner = candidate.get("id") == owner_project_id
            for segment in candidate.get("segments", []):
                request = segment.get("request", {}) if isinstance(segment, dict) else {}
                references = request.get("references", []) if isinstance(request, dict) else []
                if (not is_owner or include_owner_requests) and any(
                    isinstance(reference, dict) and reference.get("asset_id") == asset_id
                    for reference in references
                ):
                    return True
                attempts = segment.get("attempts", []) if isinstance(segment, dict) else []
                if not is_owner and any(
                    evidence.get("asset_id") == asset_id
                    for attempt in attempts
                    for evidence in self._attempt_derived_evidence(attempt)
                ):
                    return True
        # A queued/running non-owner job may not have copied its Comfy input
        # yet; never reclaim beneath it. Completed jobs retain their own hash
        # and immutable output evidence and do not need the derived binary.
        return any(
            job.get("status") in ACTIVE_JOBS
            and any(
                isinstance(reference, dict) and reference.get("asset_id") == asset_id
                for reference in job.get("references", [])
            )
            for job in self.jobs.list()
            if isinstance(job.get("references"), list)
        )

    def _enrich_output(self, output: dict[str, Any]) -> dict[str, Any]:
        if output.get("type", "output") != "output":
            raise ApiError(403, "unsafe_output", "only permanent ComfyUI outputs may be used")
        path = secure_join(self.config.comfy_output, str(output.get("subfolder", "")), str(output.get("filename", "")))
        if not path.is_file():
            raise ApiError(404, "output_file_missing", "ComfyUI output file is missing")
        return {
            **output, "size": path.stat().st_size, "sha256": self._sha256(path),
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "media": AssetStore._probe_media(path, "video"),
        }

    def _job_output(self, job: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        return self._job_output_at(job, 0)

    def _job_output_at(self, job: dict[str, Any], index: int = 0) -> tuple[Path, dict[str, Any]]:
        outputs = job.get("outputs")
        if job.get("status") != "completed" or not isinstance(outputs, list) or not outputs:
            raise ApiError(409, "continuation_not_ready", "source job has no completed output")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(outputs):
            raise ApiError(400, "media_source_output", "source job output index is out of range")
        output = outputs[index]
        if not isinstance(output, dict) or output.get("type", "output") != "output":
            raise ApiError(403, "unsafe_output", "source output is not a permanent ComfyUI output")
        path = secure_join(self.config.comfy_output, str(output.get("subfolder", "")), str(output.get("filename", "")))
        if not path.is_file():
            raise ApiError(404, "output_file_missing", "source output file is missing")
        return path, output

    def _direct_media_input(
        self, media_source: dict[str, Any],
    ) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
        if media_source.get("type") == "asset":
            asset = self.assets.get(str(media_source["asset_id"]))
            if asset.get("kind") != "video":
                raise ApiError(409, "media_source_not_video", "direct media asset is no longer a video")
            path = self.assets.content_path(asset)
            media = asset.get("media") if isinstance(asset.get("media"), dict) else AssetStore._probe_media(path, "video")
            recorded_sha = asset.get("sha256")
            identity = {"source_type": "asset", "asset_id": str(asset["id"])}
        else:
            job_id = str(media_source["job_id"])
            output_index = int(media_source.get("index", 0) or 0)
            path, output = self._job_output_at(self.jobs.get(job_id), output_index)
            media = output.get("media") if isinstance(output.get("media"), dict) else AssetStore._probe_media(path, "video")
            recorded_sha = output.get("sha256")
            identity = {"source_type": "job", "job_id": job_id, "output_index": output_index}
        current_sha = self._sha256(path)
        if not isinstance(recorded_sha, str) or re.fullmatch(r"[0-9a-f]{64}", recorded_sha) is None:
            raise ApiError(409, "output_integrity", "a direct media source has no valid recorded hash")
        if recorded_sha != current_sha:
            raise ApiError(409, "output_integrity", "a direct media source no longer matches its recorded hash")
        return path, media, current_sha, identity

    def _normalize_direct_media(
        self,
        source: Path,
        media_source: dict[str, Any],
        width: int,
        height: int,
        destination: Path,
    ) -> dict[str, Any]:
        start_frame = int(media_source["start_frame"])
        end_frame = int(media_source["end_frame"])
        source_fps = float(media_source["fps"])
        duration = (end_frame - start_frame) / source_fps
        video_filter = (
            f"trim=start_frame={start_frame}:end_frame={end_frame},"
            "setpts=PTS-STARTPTS,fps=24,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p"
        )
        command = ["ffmpeg", "-y", "-v", "error", "-i", str(source)]
        if media_source.get("keep_audio"):
            start_time = start_frame / source_fps
            end_time = end_frame / source_fps
            command.extend([
                "-filter_complex",
                f"[0:v]{video_filter}[v];[0:a:0]atrim=start={start_time:.9f}:end={end_time:.9f},asetpts=PTS-STARTPTS,aresample=48000[a]",
                "-map", "[v]", "-map", "[a]",
            ])
        else:
            command.extend([
                "-f", "lavfi", "-t", f"{duration:.9f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-filter_complex", f"[0:v]{video_filter}[v]",
                "-map", "[v]", "-map", "1:a:0",
            ])
        command.extend([
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
            "-movflags", "+faststart", str(destination),
        ])
        self._run_command(command, destination, "direct media normalization")
        return {
            "duration": duration, "video_duration": duration,
            "has_video": True, "has_audio": True,
            "video_codec": "h264", "audio_codec": "aac",
            "width": width, "height": height, "fps": 24.0,
            "frame_count": max(1, int(round(duration * 24.0))),
        }

    def _fail_attempt(self, project_id: str, index: int, attempt_id: str, error: Exception) -> None:
        message = error.message if isinstance(error, ApiError) else str(error) or "segment failed"
        with self.lock:
            project = self.store.get(project_id)
            segment = project["segments"][index]
            attempt = next(a for a in segment["attempts"] if a["id"] == attempt_id)
            attempt.update({"status": "failed", "error": message, "finished_at": time.time()})
            segment.update({"status": "failed", "error": message})
            project.update({"status": "failed", "updated_at": time.time()})
            self.store.put(project_id, project)

    def _finish_stopped(self, project: dict[str, Any]) -> None:
        with self.lock:
            current = int(project.get("current_index", -1))
            if 0 <= current < len(project.get("segments", [])) and project["segments"][current].get("status") == "running":
                segment = project["segments"][current]
                segment["status"] = "stopped"
                active = next(
                    (attempt for attempt in reversed(segment.get("attempts", [])) if attempt.get("status") in {"preparing", "submitting", "queued", "running"}),
                    None,
                )
                if active:
                    active.update({"status": "canceled", "finished_at": time.time()})
            project.update({"status": "stopped", "current_index": -1, "updated_at": time.time()})
            self.store.put(str(project["id"]), project)

    def _merge_project(self, project_id: str, attempt_id: str) -> None:
        staging: Path | None = None
        destination: Path | None = None
        concat_path: Path | None = None
        normalized_direct: list[Path] = []
        cancel_event = self._merge_cancel_events.setdefault(project_id, threading.Event())
        try:
            project = self.store.get(project_id)
            sources: list[Path] = []
            probes: list[dict[str, Any]] = []
            integrity_sources: list[Path] = []
            source_evidence: list[dict[str, Any]] = []
            inputs: list[tuple[dict[str, Any], Path, dict[str, Any], str, dict[str, Any]]] = []
            for segment in project["segments"]:
                if segment.get("kind") == "media":
                    source, media, current_sha, identity = self._direct_media_input(dict(segment["media_source"]))
                else:
                    source_job = self.jobs.get(str(segment["job_id"]))
                    source, output = self._job_output(source_job)
                    media = output.get("media") if isinstance(output.get("media"), dict) else AssetStore._probe_media(source, "video")
                    current_sha = self._sha256(source)
                    recorded_sha = output.get("sha256")
                    if not isinstance(recorded_sha, str) or re.fullmatch(r"[0-9a-f]{64}", recorded_sha) is None or recorded_sha != current_sha:
                        raise ApiError(409, "output_integrity", "a segment output no longer matches its recorded hash")
                    identity = {"source_type": "job", "job_id": str(source_job["id"]), "output_index": 0}
                inputs.append((segment, source, media, current_sha, identity))
            first = next((item[2] for item in inputs if item[0].get("kind") != "media"), inputs[0][2])
            expected = (int(first.get("width", 0)), int(first.get("height", 0)))
            if expected[0] <= 0 or expected[1] <= 0:
                raise ApiError(409, "merge_incompatible", "segment dimensions are unavailable")
            normalize_root = secure_join(self.config.data_root, "tmp")
            normalize_root.mkdir(parents=True, exist_ok=True)
            for segment, raw_source, raw_media, current_sha, identity in inputs:
                source = raw_source
                media = raw_media
                if segment.get("kind") == "media":
                    source = secure_join(normalize_root, f"direct-{project_id}-{attempt_id}-{int(segment['index'])}.mp4")
                    normalized_direct.append(source)
                    media = self._normalize_direct_media(raw_source, dict(segment["media_source"]), expected[0], expected[1], source)
                sources.append(source)
                integrity_sources.append(raw_source)
                probes.append(media)
                source_evidence.append({
                    "index": int(segment["index"]), "segment_id": str(segment["id"]),
                    **identity, "sha256": current_sha,
                    "size": source.stat().st_size,
                    "duration": float(media.get("duration", 0) or 0),
                })
            for media in probes:
                if (int(media.get("width", 0)), int(media.get("height", 0))) != expected:
                    raise ApiError(409, "merge_incompatible", "all segments must have the same dimensions")
                if not math.isclose(float(media.get("fps", 0)), 24.0, abs_tol=0.01):
                    raise ApiError(409, "merge_incompatible", "all segments must be 24 fps")
                if media.get("has_audio") is not True:
                    raise ApiError(409, "merge_incompatible", "all segments must include an audio track")
                if str(media.get("video_codec", "")).lower() not in {"h264", "avc1"}:
                    raise ApiError(409, "merge_incompatible", "all segments must use the normalized H.264 video codec")
                if str(media.get("audio_codec", "")).lower() != "aac":
                    raise ApiError(409, "merge_incompatible", "all segments must use the normalized AAC audio codec")
            relative_dir = Path("h3-studio") / "projects" / project_id
            output_dir = secure_join(self.config.comfy_output, *relative_dir.parts)
            output_dir.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            staging = secure_join(output_dir, f".partial-{token}.mp4")
            destination = secure_join(output_dir, f"merged-{token}.mp4")
            concat_path = secure_join(self.config.data_root / "tmp", f"concat-{project_id}-{attempt_id}.ffconcat")
            concat_text = "ffconcat version 1.0\n" + "".join(
                f"file '{self._ffconcat_escape(source)}'\n" for source in sources
            )
            concat_path.write_text(concat_text, encoding="utf-8")

            expected_bytes = sum(item["size"] for item in source_evidence) + 16 * 1024 * 1024
            recipe_finalization = (
                isinstance(project.get("recipe"), dict)
                and project["recipe"].get("type") in {
                    CHARACTER_MIGRATION_RECIPE_TYPE, REPLICATION_RECIPE_TYPE,
                }
            )
            disk_required_bytes = expected_bytes * (2 if recipe_finalization else 1)
            merged_root = secure_join(self.config.comfy_output, "h3-studio", "projects")
            merged_root.mkdir(parents=True, exist_ok=True)
            staging_relative = staging.relative_to(self.config.comfy_output.resolve()).as_posix()
            destination_relative = destination.relative_to(self.config.comfy_output.resolve()).as_posix()
            concat_relative = concat_path.relative_to(self.config.data_root.resolve()).as_posix()
            with self.lock:
                # Quota observation and reservation are one transaction. Two
                # concurrent merges must never both spend the same capacity.
                existing_bytes = sum(path.stat().st_size for path in merged_root.rglob("merged-*.mp4") if path.is_file())
                active_reserved = sum(
                    int(candidate_attempt.get("reserved_bytes", 0) or 0)
                    for candidate in self.store.list()
                    for candidate_attempt in candidate.get("merge_attempts", [])
                    if candidate_attempt.get("status") == "merging" and candidate.get("id") != project_id
                )
                if existing_bytes + active_reserved + expected_bytes > self.config.max_merged_output_bytes:
                    raise ApiError(507, "merge_quota", "merged-video output quota would be exceeded")
                if shutil.disk_usage(output_dir).free < disk_required_bytes:
                    raise ApiError(507, "disk_full", "insufficient free disk space for merged output")
                latest = self.store.get(project_id)
                attempt = next(a for a in latest.get("merge_attempts", []) if a["id"] == attempt_id)
                attempt.update({
                    "sources": source_evidence, "reserved_bytes": expected_bytes,
                    "progress": 0,
                    "staging_relative_path": staging_relative,
                    "destination_relative_path": destination_relative,
                    "concat_relative_path": concat_relative,
                })
                latest["merged"] = {"status": "merging", "progress": 0, "sources": source_evidence}
                latest["updated_at"] = time.time()
                self.store.put(project_id, latest)

            total_duration = sum(item["duration"] for item in source_evidence)
            timeout = max(
                self.config.merge_timeout_min_seconds,
                int(math.ceil(total_duration * self.config.merge_timeout_factor + 120)),
            )
            command = [
                "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", str(concat_path), "-map", "0:v:0", "-map", "0:a:0",
                "-c", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(staging),
            ]
            self._run_merge_command(project_id, command, staging, timeout, cancel_event, total_duration)
            finalization: dict[str, Any] | None = None
            if recipe_finalization:
                staging, finalization = self._finalize_recipe_output(
                    project, staging, expected, attempt_id, cancel_event,
                )
            media = AssetStore._probe_media(staging, "video")
            if finalization is not None:
                expected_frames = int(finalization["frames"])
                if int(media.get("frame_count", 0) or 0) != expected_frames:
                    raise ApiError(422, "project_trim_failed", f"final output must contain exactly {expected_frames} frames")
                expected_audio = finalization["audio_policy"] != "mute"
                if (media.get("has_audio") is True) != expected_audio:
                    raise ApiError(422, "project_audio_failed", "final output stream layout does not match audio_policy")
            staged_sha = self._sha256(staging)
            # Detect replacement while ffmpeg was reading, not just before it.
            for source, evidence in zip(integrity_sources, source_evidence, strict=True):
                if self._sha256(source) != evidence["sha256"]:
                    raise ApiError(409, "output_integrity", "a segment output changed during merge")
            if cancel_event.is_set():
                raise MergeCanceled("merge canceled by user")
            os.replace(staging, destination)
            relative_path = destination.relative_to(self.config.comfy_output.resolve()).as_posix()
            output_evidence = {
                "filename": destination.name,
                "subfolder": Path(relative_path).parent.as_posix(),
                "type": "output", "sha256": staged_sha,
                "size": destination.stat().st_size, "mime_type": "video/mp4", "media": media,
            }
            result_job_id = uuid.uuid4().hex
            now = time.time()
            self.jobs.put(result_job_id, {
                "id": result_job_id, "job_id": result_job_id, "request_id": uuid.uuid4().hex,
                "prompt_id": None, "client_id": None, "status": "completed", "output_type": "video",
                "raw_prompt": project.get("title", ""), "prompt": project.get("title", ""),
                "prompt_parts": {}, "parameters": {
                    "kind": "merged_video_project", "segment_count": len(sources),
                    "width": expected[0], "height": expected[1], "fps": 24,
                    **(
                        {str(project["recipe"]["type"]): finalization}
                        if finalization is not None and isinstance(project.get("recipe"), dict) else {}
                    ),
                },
                "references": [], "video_project_id": project_id, "synthetic_merge": True,
                "source_evidence": source_evidence,
                "outputs": [output_evidence], "created_at": now, "updated_at": now,
            })
            merged = {
                "status": "completed", "relative_path": relative_path,
                "progress": 100,
                "sha256": output_evidence["sha256"], "size": destination.stat().st_size,
                "media": media, "result_job_id": result_job_id, "sources": source_evidence,
                **({"finalization": finalization} if finalization is not None else {}),
            }
            with self.lock:
                project = self.store.get(project_id)
                attempt = next(a for a in project.get("merge_attempts", []) if a["id"] == attempt_id)
                attempt.update({**merged, "finished_at": time.time()})
                project.update({"merged": merged, "status": "completed", "updated_at": time.time()})
                self.store.put(project_id, project)
        except MergeCanceled as error:
            if staging:
                staging.unlink(missing_ok=True)
            if destination:
                destination.unlink(missing_ok=True)
            with self.lock:
                project = self.store.get(project_id)
                attempt = next(a for a in project.get("merge_attempts", []) if a["id"] == attempt_id)
                attempt.update({"status": "canceled", "error": str(error), "finished_at": time.time()})
                project.update({"merged": {"status": "canceled", "error": str(error)}, "status": "stopped", "current_index": -1, "updated_at": time.time()})
                self.store.put(project_id, project)
        except Exception as error:
            if staging:
                staging.unlink(missing_ok=True)
            if destination:
                destination.unlink(missing_ok=True)
            message = error.message if isinstance(error, ApiError) else str(error) or "merge failed"
            with self.lock:
                project = self.store.get(project_id)
                attempt = next(a for a in project.get("merge_attempts", []) if a["id"] == attempt_id)
                attempt.update({"status": "failed", "error": message, "finished_at": time.time()})
                project.update({"merged": {"status": "failed", "error": message}, "status": "failed", "updated_at": time.time()})
                self.store.put(project_id, project)
        finally:
            if concat_path:
                concat_path.unlink(missing_ok=True)
            for direct in normalized_direct:
                direct.unlink(missing_ok=True)
            self._merge_processes.pop(project_id, None)
            self._merge_cancel_events.pop(project_id, None)

    @staticmethod
    def _ffconcat_escape(path: Path) -> str:
        return str(path.resolve()).replace("'", "'\\''")

    def _finalize_recipe_output(
        self,
        project: dict[str, Any],
        concatenated: Path,
        dimensions: tuple[int, int],
        attempt_id: str,
        cancel_event: threading.Event,
    ) -> tuple[Path, dict[str, Any]]:
        """Trim padded tail frames and apply a durable recipe audio policy."""
        self._validate_recipe_assets(project)
        recipe = validate_project_recipe(project.get("recipe"))
        recipe_type = str(recipe.get("type", "project"))
        segmentation = recipe.get("segmentation") if isinstance(recipe.get("segmentation"), dict) else {}
        output = recipe.get("output") if isinstance(recipe.get("output"), dict) else {}
        expected_dimensions = (int(output.get("width", 0) or 0), int(output.get("height", 0) or 0))
        if dimensions != expected_dimensions:
            raise ApiError(
                409, "project_dimensions",
                "generated segment dimensions do not match the planned project output",
            )
        frames = int(segmentation.get("source_frames", output.get("frames", 0)) or 0)
        fps = int(segmentation.get("fps", 24) or 0)
        if frames <= 0 or fps != 24:
            raise ApiError(409, "invalid_project_recipe", "recipe must retain a positive 24fps final frame count")
        duration = frames / 24.0
        audio_policy = str(recipe.get("audio_policy", ""))
        processed = concatenated.with_name(f"{concatenated.stem}-final-{uuid.uuid4().hex}.mp4")
        video_filter = f"trim=start_frame=0:end_frame={frames},setpts=PTS-STARTPTS,fps=24"
        command = ["ffmpeg", "-y", "-v", "error", "-i", str(concatenated)]
        audio_index = 0
        source_path: Path | None = None
        source_sha: str | None = None
        if audio_policy == "copy-source":
            source = self.assets.get(str(recipe["source_asset_id"]))
            media = source.get("media") if isinstance(source.get("media"), dict) else {}
            if media.get("has_audio") is not True:
                raise ApiError(422, "audio_stream_missing", "copy-source requires a usable source audio stream")
            source_path = self.assets.content_path(source)
            source_sha = self._sha256(source_path)
            if source_sha != recipe.get("source_sha256"):
                raise ApiError(409, "source_integrity", "source video changed before final audio mux")
            command.extend(["-i", str(source_path)])
            audio_index = 1
        if audio_policy == "mute":
            command.extend(["-vf", video_filter, "-map", "0:v:0", "-an"])
        else:
            command.extend([
                "-filter_complex",
                (
                    f"[0:v:0]{video_filter}[v];"
                    f"[{audio_index}:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
                    f"apad=pad_dur={duration:.9f},atrim=duration={duration:.9f}[a]"
                ),
                "-map", "[v]", "-map", "[a]",
            ])
        command.extend([
            "-frames:v", str(frames), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p",
        ])
        if audio_policy != "mute":
            command.extend(["-c:a", "aac", "-ar", "48000", "-ac", "2"])
        command.extend(["-t", f"{duration:.9f}", "-movflags", "+faststart", str(processed)])
        try:
            with self.lock:
                latest = self.store.get(str(project["id"]))
                attempt = next(item for item in latest.get("merge_attempts", []) if item.get("id") == attempt_id)
                attempt["postprocess_relative_path"] = processed.resolve().relative_to(
                    self.config.comfy_output.resolve()
                ).as_posix()
                latest["updated_at"] = time.time()
                self.store.put(str(project["id"]), latest)
            self._run_merge_command(
                str(project["id"]), command, processed,
                max(300, int(duration * 2 + 120)), cancel_event, duration,
            )
            if source_path is not None and self._sha256(source_path) != source_sha:
                raise ApiError(409, "source_integrity", "source video changed during final audio mux")
            concatenated.unlink(missing_ok=True)
            return processed, {
                "type": recipe_type, "version": recipe["version"], "frames": frames, "fps": 24,
                "duration": duration, "width": dimensions[0], "height": dimensions[1],
                "removed_tail_frames": int(segmentation.get("final_trim_frames", 0) or 0),
                "audio_policy": audio_policy,
                "audio_end_behavior": "pad_or_trim_to_exact_video_duration" if audio_policy != "mute" else "removed",
            }
        except Exception:
            processed.unlink(missing_ok=True)
            raise

    def _finalize_character_migration(
        self,
        project: dict[str, Any],
        concatenated: Path,
        dimensions: tuple[int, int],
        attempt_id: str,
        cancel_event: threading.Event,
    ) -> tuple[Path, dict[str, Any]]:
        """Backward-compatible entry point used by existing tests and integrations."""
        return self._finalize_recipe_output(
            project, concatenated, dimensions, attempt_id, cancel_event,
        )

    def _run_merge_command(
        self,
        project_id: str,
        command: list[str],
        output: Path,
        timeout: int,
        cancel_event: threading.Event,
        total_duration: float,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        if cancel_event.is_set():
            raise MergeCanceled("merge canceled by user")
        if self.command_runner is not subprocess.run:
            self._run_command(command, output, "video merge", timeout=timeout)
            if cancel_event.is_set():
                output.unlink(missing_ok=True)
                raise MergeCanceled("merge canceled by user")
            return
        deadline = time.monotonic() + timeout
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            )
        except OSError as error:
            raise ApiError(422, "ffmpeg_failed", "ffmpeg could not start video merge") from error
        self._merge_processes[project_id] = process
        progress_thread = threading.Thread(
            target=self._read_merge_progress,
            args=(project_id, process, total_duration),
            name=f"h3-merge-progress-{project_id[:8]}", daemon=True,
        )
        progress_thread.start()
        def close_pipes() -> None:
            progress_thread.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.read()
                process.stderr.close()
        while process.poll() is None:
            if cancel_event.wait(0.1):
                self._terminate_process_group(process)
                close_pipes()
                output.unlink(missing_ok=True)
                raise MergeCanceled("merge canceled by user")
            if time.monotonic() >= deadline:
                self._terminate_process_group(process)
                close_pipes()
                output.unlink(missing_ok=True)
                raise ApiError(422, "ffmpeg_failed", "ffmpeg timed out during video merge")
        close_pipes()
        if cancel_event.is_set():
            output.unlink(missing_ok=True)
            raise MergeCanceled("merge canceled by user")
        if process.returncode or not output.is_file() or output.stat().st_size == 0:
            output.unlink(missing_ok=True)
            raise ApiError(422, "ffmpeg_failed", "ffmpeg failed during video merge")

    def _read_merge_progress(
        self, project_id: str, process: subprocess.Popen[str], total_duration: float,
    ) -> None:
        if process.stdout is None or total_duration <= 0:
            return
        last_progress = -1
        for raw in process.stdout:
            line = raw.strip()
            if not (line.startswith("out_time_us=") or line.startswith("out_time_ms=")):
                continue
            try:
                processed_seconds = int(line.split("=", 1)[1]) / 1_000_000
            except (ValueError, IndexError):
                continue
            progress = max(0, min(99, int(processed_seconds / total_duration * 100)))
            if progress <= last_progress:
                continue
            last_progress = progress
            with self.lock:
                try:
                    project = self.store.get(project_id)
                except ApiError:
                    return
                attempt = next(
                    (item for item in reversed(project.get("merge_attempts", [])) if item.get("status") == "merging"),
                    None,
                )
                if attempt is None:
                    return
                attempt["progress"] = progress
                if isinstance(project.get("merged"), dict):
                    project["merged"]["progress"] = progress
                project["updated_at"] = time.time()
                self.store.put(project_id, project)

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
        """Signal the isolated ffmpeg process group, with a portable fallback."""
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[str], grace_seconds: float = 5) -> None:
        """Terminate the whole isolated group, including stubborn descendants."""
        if os.name != "posix":
            process.terminate()
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=grace_seconds)
            return
        pgid = process.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            process.terminate()
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                # Some sandboxed POSIX hosts report EPERM after the signaled
                # group has already disappeared.  There is no further group
                # signal available to us on that host.
                break
            time.sleep(0.05)
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        try:
            process.wait(timeout=max(1.0, grace_seconds))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=max(1.0, grace_seconds))

    def _cleanup_merge_paths(self, project: dict[str, Any]) -> None:
        for attempt in project.get("merge_attempts", []):
            if attempt.get("status") != "merging":
                continue
            for key in ("staging_relative_path", "postprocess_relative_path", "destination_relative_path"):
                relative = attempt.get(key)
                if isinstance(relative, str):
                    try:
                        secure_join(self.config.comfy_output, relative).unlink(missing_ok=True)
                    except ApiError:
                        pass
            relative = attempt.get("concat_relative_path")
            if isinstance(relative, str):
                try:
                    secure_join(self.config.data_root, relative).unlink(missing_ok=True)
                except ApiError:
                    pass

    def _run_command(self, command: list[str], output: Path, label: str, *, timeout: int = 300) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            completed = self.command_runner(command, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            output.unlink(missing_ok=True)
            raise ApiError(422, "ffmpeg_failed", f"ffmpeg could not complete {label}") from error
        if completed.returncode or not output.is_file() or output.stat().st_size == 0:
            output.unlink(missing_ok=True)
            raise ApiError(422, "ffmpeg_failed", f"ffmpeg failed during {label}")

    def _get(self, project_id: str) -> dict[str, Any]:
        return self.store.get(validate_id(project_id, "project id"))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
