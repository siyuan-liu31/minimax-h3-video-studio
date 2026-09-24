"""Deterministic planning for the native H3 replication workshop."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from .errors import ApiError
from .profiles import H3_MAX_FRAMES, ProfileRegistry, WorkflowProfile
from .security import validate_id


SCHEMA_VERSION = "h3.replication/v1"
RECIPE_TYPE = "replication"
LEGAL_SEGMENT_FRAMES = tuple(range(124, H3_MAX_FRAMES + 1, 17))
AUDIO_POLICIES = ("copy-source", "reference-source", "generate", "mute")
CONTINUITY_MODES = ("auto", "none", "motion_context")
PRESERVE_OPTIONS = (
    "timing", "motion", "camera", "composition", "environment", "lighting", "interactions",
)
MAX_BRIEF_LENGTH = 4_000
MAX_REPLACEMENT_LENGTH = 2_000
MAX_PROMPT_LENGTH = 12_000
MAX_REFERENCE_ASSETS = 11
DEFAULT_MAX_PROJECT_BYTES = 32 * 1024 * 1024


def capability(*, profiles: list[dict[str, Any]], motion_context: dict[str, Any]) -> dict[str, Any]:
    supported = [
        {
            "id": item.get("id"),
            "version": item.get("version"),
            "manifest_sha256": item.get("manifest_sha256"),
            "sampling_mode": item.get("sampling_mode"),
            "available": item.get("available") is True,
        }
        for item in profiles
        if item.get("compiler") == "h3_ref"
    ]
    return {
        "available": any(item["available"] for item in supported),
        "recipe_version": SCHEMA_VERSION,
        "supported_profiles": supported,
        "segment_frames": list(LEGAL_SEGMENT_FRAMES),
        "audio_policies": list(AUDIO_POLICIES),
        "continuity_modes": list(CONTINUITY_MODES),
        "preserve_options": list(PRESERVE_OPTIONS),
        "limits": {
            "fps": 24,
            "source_duration_seconds": [1 / 24, None],
            "max_reference_assets": MAX_REFERENCE_ASSETS,
            "max_segment_frames": H3_MAX_FRAMES,
        },
        "motion_context": {
            "available": motion_context.get("available") is True,
            "optional": True,
        },
    }


def _text(value: Any, field: str, *, maximum: int, required: bool = False, preserve: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, "invalid_replication", f"{field} must be a string")
    result = value if preserve else value.strip()
    if required and not result.strip():
        raise ApiError(400, "invalid_replication", f"{field} must not be empty")
    if len(result) > maximum:
        raise ApiError(400, "invalid_replication", f"{field} must contain at most {maximum} characters")
    return result


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_replication", f"{field} must be an integer")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ApiError(400, "invalid_replication", f"{field} must be a finite number")
    return float(value)


def _profile_number(profile: WorkflowProfile, key: str, value: float) -> None:
    limits = profile.limits.get(key)
    if not isinstance(limits, (list, tuple)) or len(limits) != 2:
        return
    minimum, maximum = float(limits[0]), float(limits[1])
    if value < minimum or value > maximum:
        raise ApiError(
            400, "invalid_replication",
            f"{key} received {value:g}; allowed range for profile {profile.id} is {minimum:g}..{maximum:g}",
        )


def _display_dimensions(media: dict[str, Any]) -> tuple[int, int]:
    width = int(media.get("width", 0) or 0)
    height = int(media.get("height", 0) or 0)
    if int(media.get("rotation", 0) or 0) % 360 in {90, 270}:
        width, height = height, width
    if width <= 0 or height <= 0:
        raise ApiError(422, "source_dimensions_missing", "source video display dimensions are unavailable")
    return width, height


def _output_dimensions(media: dict[str, Any]) -> tuple[str, int, int]:
    width, height = _display_dimensions(media)
    ratio = width / height
    if ratio > 1.15:
        return "16:9", 1344, 768
    if ratio < 0.87:
        return "9:16", 768, 1344
    return "1:1", 1024, 1024


def _source_frame_count(media: dict[str, Any]) -> int:
    duration = _number(media.get("video_duration") or media.get("duration"), "source duration")
    if duration <= 0 or not math.isfinite(duration * 24):
        raise ApiError(400, "replication_duration", "source video duration must be finite and positive")
    frames = int(round(duration * 24.0))
    if frames <= 0:
        raise ApiError(422, "source_duration_missing", "source video has no frames on the 24 fps timeline")
    return frames


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _validate_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ApiError(400, "invalid_replication_recipe", f"{field} must be 64 lowercase hex characters")
    return value


def _normalize_preserve(value: Any) -> list[str]:
    if value is None:
        return ["timing", "motion", "camera", "composition"]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ApiError(400, "invalid_replication", "preserve must be an array of strings")
    result: list[str] = []
    for item in value:
        if item not in PRESERVE_OPTIONS:
            raise ApiError(400, "invalid_replication", f"unsupported preserve option: {item}")
        if item not in result:
            result.append(item)
    return result


def _normalize_replace(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    allowed = {"subject", "product", "setting", "script", "language", "style"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ApiError(400, "invalid_replication", "replace contains unsupported fields")
    result: dict[str, str] = {}
    for key, item in value.items():
        normalized = _text(item, f"replace.{key}", maximum=MAX_REPLACEMENT_LENGTH)
        if normalized:
            result[key] = normalized
    return result


def _normalize_cut_frames(value: Any, source_frames: int) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 200:
        raise ApiError(400, "invalid_replication", "cut_frames must be an array of at most 200 integers")
    result: list[int] = []
    previous = 0
    for item in value:
        frame = _integer(item, "cut_frames item")
        if frame <= previous or frame >= source_frames:
            raise ApiError(400, "invalid_replication", "cut_frames must be strictly increasing inside the source video")
        result.append(frame)
        previous = frame
    return result


def _segment_windows(source_frames: int, cut_frames: list[int], overlap: int = 0) -> list[tuple[int, int, int]]:
    """Return source windows and legal generated-frame lengths.

    Windows own disjoint source frames. Continuations generate an additional
    overlap head, which Motion Context trims before composition. Only the
    terminal window is padded and trimmed once after merge.
    Scene cuts only influence which legal boundary is selected.
    """
    stride = H3_MAX_FRAMES - overlap
    count = max(1, (source_frames - overlap + stride - 1) // stride)
    cursor = 0
    windows: list[tuple[int, int, int]] = []
    for index in range(count - 1):
        remaining_segments = count - index - 1
        head = overlap if index else 0
        minimum = source_frames - cursor - remaining_segments * stride
        maximum = source_frames - cursor - (remaining_segments - 1) * (LEGAL_SEGMENT_FRAMES[0] - overlap) - 1
        candidates = [frames - head for frames in LEGAL_SEGMENT_FRAMES if minimum <= frames - head <= maximum]
        target = (source_frames - cursor) / (remaining_segments + 1)
        scene_candidates = [cut - cursor for cut in cut_frames if minimum <= cut - cursor <= maximum]
        choice_pool = [candidate for candidate in scene_candidates if candidate in candidates] or candidates
        length = min(choice_pool, key=lambda frames: (abs(frames - target), -frames))
        windows.append((cursor, cursor + length, length + head))
        cursor += length
    remaining = source_frames - cursor
    head = overlap if count > 1 else 0
    generated = next(frames for frames in LEGAL_SEGMENT_FRAMES if frames >= remaining + head)
    windows.append((cursor, source_frames, generated))
    return windows


def validate_execution(project: dict[str, Any]) -> None:
    """Reject stale or edited frame accounting before spending GPU time."""
    recipe = project.get("recipe")
    if not isinstance(recipe, dict) or recipe.get("type") != RECIPE_TYPE:
        return
    validate_recipe(recipe)
    segmentation = recipe["segmentation"]
    windows = segmentation["windows"]
    overlap = segmentation.get("motion_context_frames", 0)
    if recipe["continuity"] == "motion_context" and len(windows) > 1 and overlap != 22:
        raise ApiError(409, "replication_replan_required", "this legacy replication plan omits Motion Context trimming; create a new plan")
    segments = project.get("segments", [])
    valid = len(segments) == len(windows)
    for index, (segment, window) in enumerate(zip(segments, windows)):
        head = overlap if index else 0
        expected_mode = recipe["continuity"] if index else "none"
        source_range = segment.get("source_range", {})
        parameters = segment.get("request", {}).get("parameters", {})
        valid = valid and (
            segment.get("continuation", "none") == expected_mode
            and source_range == {"asset_id": recipe["source_asset_id"], "start_frame": window["start_frame"] - head, "end_frame": window["end_frame"], "fps": 24.0}
            and round(float(parameters.get("duration", 0)) * 24) == window["generated_frames"]
            and (not head or segment.get("motion_context", {}).get("video_frames", 22) == head)
        )
    if not valid:
        raise ApiError(409, "replication_replan_required", "replication segments no longer match the recipe frame ownership; create a new plan")


def build_prompt(
    *, brief: str, preserve: list[str], replace: dict[str, str], references: list[dict[str, str]],
    expert_prompt: str = "",
) -> str:
    if expert_prompt:
        return expert_prompt
    clauses = [brief.rstrip(". ") + "."]
    if preserve:
        clauses.append("Preserve the source video's " + ", ".join(preserve) + ".")
    for key, value in replace.items():
        clauses.append(f"Replace {key} with: {value}.")
    if references:
        clauses.append(
            "Use " + ", ".join(f"@{{{item['asset_id']}}} as {item['role']}" for item in references) + "."
        )
    clauses.append("Keep identities and objects temporally consistent; avoid morphing, duplication, flicker, and unintended drift.")
    return " ".join(clauses)


def validate_recipe(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_replication_recipe", "recipe must be an object")
    allowed = {
        "type", "version", "source_asset_id", "source_sha256", "brief", "preserve", "replace",
        "references", "prompt_policy", "segmentation", "continuity", "audio_policy", "output",
    }
    if set(value) - allowed:
        raise ApiError(400, "invalid_replication_recipe", f"unsupported recipe field: {sorted(set(value) - allowed)[0]}")
    if value.get("type") != RECIPE_TYPE or value.get("version") != SCHEMA_VERSION:
        raise ApiError(400, "invalid_replication_recipe", f"recipe type/version must be {RECIPE_TYPE}/{SCHEMA_VERSION}")
    validate_id(value.get("source_asset_id"), "recipe source_asset_id")
    _validate_sha(value.get("source_sha256"), "recipe source_sha256")
    _text(value.get("brief"), "recipe brief", maximum=MAX_BRIEF_LENGTH, required=True)
    preserve = _normalize_preserve(value.get("preserve"))
    replace = _normalize_replace(value.get("replace"))
    references = value.get("references")
    if not isinstance(references, list) or len(references) > MAX_REFERENCE_ASSETS:
        raise ApiError(400, "invalid_replication_recipe", "recipe references must be an array within the reference limit")
    seen: set[str] = set()
    for item in references:
        if not isinstance(item, dict) or set(item) != {"asset_id", "sha256", "role"}:
            raise ApiError(400, "invalid_replication_recipe", "each recipe reference requires asset_id, sha256, and role")
        asset_id = validate_id(item.get("asset_id"), "recipe reference asset_id")
        if asset_id in seen:
            raise ApiError(400, "invalid_replication_recipe", "recipe reference assets must be unique")
        seen.add(asset_id)
        _validate_sha(item.get("sha256"), "recipe reference sha256")
        _text(item.get("role"), "recipe reference role", maximum=100, required=True)
    prompt_policy = value.get("prompt_policy")
    if not isinstance(prompt_policy, dict) or set(prompt_policy) != {"mode", "prompt_sha256"}:
        raise ApiError(400, "invalid_replication_recipe", "recipe prompt_policy is invalid")
    if prompt_policy.get("mode") not in {"deterministic-v1", "expert"}:
        raise ApiError(400, "invalid_replication_recipe", "recipe prompt policy mode is unsupported")
    _validate_sha(prompt_policy.get("prompt_sha256"), "recipe prompt sha256")
    segmentation = value.get("segmentation")
    required = {"fps", "source_frames", "windows", "composed_frames", "final_trim_frames"}
    if not isinstance(segmentation, dict) or not required <= set(segmentation) or set(segmentation) - required - {"motion_context_frames"}:
        raise ApiError(400, "invalid_replication_recipe", "recipe segmentation fields are incomplete or unsupported")
    overlap = _integer(segmentation.get("motion_context_frames", 0), "recipe motion_context_frames")
    if overlap not in {0, 22} or (overlap and value.get("continuity") != "motion_context"):
        raise ApiError(400, "invalid_replication_recipe", "recipe motion_context_frames is inconsistent")
    fps = _integer(segmentation.get("fps"), "recipe segmentation fps")
    source_frames = _integer(segmentation.get("source_frames"), "recipe segmentation source_frames")
    windows = segmentation.get("windows")
    if fps != 24 or source_frames <= 0 or not isinstance(windows, list) or not windows:
        raise ApiError(400, "invalid_replication_recipe", "recipe segmentation requires a positive 24 fps timeline")
    cursor = 0
    composed = 0
    for index, window in enumerate(windows):
        if not isinstance(window, dict) or set(window) != {"start_frame", "end_frame", "generated_frames"}:
            raise ApiError(400, "invalid_replication_recipe", "recipe window fields are invalid")
        start = _integer(window.get("start_frame"), "recipe window start_frame")
        end = _integer(window.get("end_frame"), "recipe window end_frame")
        generated = _integer(window.get("generated_frames"), "recipe window generated_frames")
        contribution = generated - (overlap if index else 0)
        if start != cursor or end <= start or end > source_frames or generated not in LEGAL_SEGMENT_FRAMES or end - start > contribution:
            raise ApiError(400, "invalid_replication_recipe", f"recipe window {index + 1} is inconsistent")
        if index < len(windows) - 1 and end - start != contribution:
            raise ApiError(400, "invalid_replication_recipe", "only the final recipe window may require padding")
        cursor = end
        composed += contribution
    if cursor != source_frames:
        raise ApiError(400, "invalid_replication_recipe", "recipe windows must cover the complete source")
    if _integer(segmentation.get("composed_frames"), "recipe composed_frames") != composed:
        raise ApiError(400, "invalid_replication_recipe", "recipe composed_frames is inconsistent")
    if _integer(segmentation.get("final_trim_frames"), "recipe final_trim_frames") != composed - source_frames:
        raise ApiError(400, "invalid_replication_recipe", "recipe final_trim_frames is inconsistent")
    if value.get("continuity") not in {"none", "motion_context"}:
        raise ApiError(400, "invalid_replication_recipe", "recipe continuity is unsupported")
    if value.get("audio_policy") not in AUDIO_POLICIES:
        raise ApiError(400, "invalid_replication_recipe", "recipe audio_policy is unsupported")
    output = value.get("output")
    if not isinstance(output, dict) or set(output) != {"aspect_ratio", "width", "height", "frames", "duration"}:
        raise ApiError(400, "invalid_replication_recipe", "recipe output fields are invalid")
    expected = {"16:9": (1344, 768), "9:16": (768, 1344), "1:1": (1024, 1024)}
    if output.get("aspect_ratio") not in expected:
        raise ApiError(400, "invalid_replication_recipe", "recipe output aspect_ratio is unsupported")
    width = _integer(output.get("width"), "recipe output width")
    height = _integer(output.get("height"), "recipe output height")
    frames = _integer(output.get("frames"), "recipe output frames")
    duration = _number(output.get("duration"), "recipe output duration")
    if (width, height) != expected[output["aspect_ratio"]] or frames != source_frames or not math.isclose(duration, frames / 24, abs_tol=1e-9):
        raise ApiError(400, "invalid_replication_recipe", "recipe output does not match segmentation")
    normalized = json.loads(json.dumps(value))
    normalized["preserve"] = preserve
    normalized["replace"] = replace
    return normalized


def plan(
    data: Any,
    *,
    source: dict[str, Any],
    reference_assets: list[dict[str, Any]],
    registry: ProfileRegistry,
    available_profiles: set[str] | None = None,
    motion_context_available: bool = True,
    max_project_bytes: int = DEFAULT_MAX_PROJECT_BYTES,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_replication", "replication input must be an object")
    allowed = {
        "version", "source_asset_id", "brief", "title", "preserve", "replace", "references",
        "profile_id", "profile_version", "profile_digest", "steps", "lora_strength", "seed",
        "continuity", "audio_policy", "cut_frames", "prompt",
    }
    if set(data) - allowed:
        raise ApiError(400, "invalid_replication", f"unknown field: {sorted(set(data) - allowed)[0]}")
    if data.get("version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ApiError(400, "unsupported_replication_version", f"version must be {SCHEMA_VERSION}")
    source_id = validate_id(data.get("source_asset_id"), "source_asset_id")
    if source.get("id") != source_id or source.get("kind") != "video":
        raise ApiError(400, "source_media_kind", "source_asset_id must identify a readable video")
    brief = _text(data.get("brief"), "brief", maximum=MAX_BRIEF_LENGTH, required=True)
    title = _text(data.get("title", "Replication workshop"), "title", maximum=200, required=True)
    preserve = _normalize_preserve(data.get("preserve"))
    replace = _normalize_replace(data.get("replace"))
    raw_references = data.get("references", [])
    if not isinstance(raw_references, list) or len(raw_references) > MAX_REFERENCE_ASSETS:
        raise ApiError(400, "invalid_replication", f"references must contain at most {MAX_REFERENCE_ASSETS} items")
    assets_by_id = {str(asset.get("id")): asset for asset in reference_assets}
    references: list[dict[str, str]] = []
    recipe_references: list[dict[str, str]] = []
    seen = {source_id}
    kind_counts = {"image": 0, "video": 1, "audio": 0}
    for item in raw_references:
        if not isinstance(item, dict) or set(item) != {"asset_id", "role"}:
            raise ApiError(400, "invalid_replication", "each reference requires only asset_id and role")
        asset_id = validate_id(item.get("asset_id"), "reference asset_id")
        if asset_id in seen:
            raise ApiError(400, "invalid_replication", "reference assets must be unique and cannot repeat the source")
        asset = assets_by_id.get(asset_id)
        if asset is None or asset.get("kind") not in {"image", "video", "audio"}:
            raise ApiError(400, "reference_media_kind", "reference asset must be image, video, or audio")
        kind = str(asset["kind"])
        kind_counts[kind] += 1
        kind_limits = {"image": 9, "video": 3, "audio": 3}
        if kind_counts[kind] > kind_limits[kind]:
            raise ApiError(400, "reference_capacity", f"replication supports at most {kind_limits[kind]} {kind} references including the source range")
        role = _text(item.get("role"), "reference role", maximum=100, required=True)
        sha = asset.get("sha256")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            raise ApiError(409, "reference_integrity", "reference asset has no persisted SHA-256")
        valid_generation_roles = {
            "image": {"first_frame", "last_frame", "identity", "style", "composition", "reference"},
            "video": {"motion", "camera", "pacing", "reference"},
            "audio": {"music", "rhythm", "reference"},
        }
        request_role = role if role in valid_generation_roles[kind] else "reference"
        references.append({"asset_id": asset_id, "role": request_role})
        recipe_references.append({"asset_id": asset_id, "sha256": sha, "role": role})
        seen.add(asset_id)
    profile_id = _text(data.get("profile_id", "minimax-h3-ref2va"), "profile_id", maximum=200, required=True)
    profile = registry.get(profile_id)
    if profile.output_type != "video" or profile.compiler != "h3_ref":
        raise ApiError(400, "replication_profile", "profile_id must select an H3 Ref2VA Base or Turbo profile")
    requested_version = str(data.get("profile_version") or "")
    requested_digest = str(data.get("profile_digest") or "")
    identity_matches = (
        profile.accepts_identity(requested_version, requested_digest)
        if requested_version and requested_digest
        else (not requested_version or profile.accepts_version(requested_version))
        and (not requested_digest or profile.accepts_digest(requested_digest))
    )
    if not identity_matches:
        raise ApiError(409, "profile_version_mismatch", "profile_version/profile_digest no longer match the selected profile")
    if available_profiles is not None and profile.id not in available_profiles:
        raise ApiError(503, "profile_unavailable", f"profile_id {profile.id!r} is unavailable")
    steps = _integer(data.get("steps", int(profile.defaults.get("steps", 4))), "steps")
    _profile_number(profile, "steps", float(steps))
    lora_strength = _number(data.get("lora_strength", profile.defaults.get("lora_strength", 0)), "lora_strength")
    if profile.sampling_mode != "turbo4" and not math.isclose(lora_strength, 0.0):
        raise ApiError(400, "invalid_replication", "lora_strength must be 0 for a Base profile")
    _profile_number(profile, "lora_strength", lora_strength)
    seed = _integer(data.get("seed", -1), "seed")
    if seed < -1:
        raise ApiError(400, "invalid_replication", "seed must be -1 or a non-negative integer")
    continuity = data.get("continuity", "auto")
    if continuity not in CONTINUITY_MODES:
        raise ApiError(400, "invalid_replication", f"continuity must be one of {', '.join(CONTINUITY_MODES)}")
    effective_continuity = "motion_context" if continuity == "auto" and motion_context_available else continuity
    if effective_continuity == "auto":
        effective_continuity = "none"
    if effective_continuity == "motion_context" and not motion_context_available:
        raise ApiError(503, "motion_context_unavailable", "motion_context continuity is unavailable")
    audio_policy = data.get("audio_policy", "copy-source")
    if audio_policy not in AUDIO_POLICIES:
        raise ApiError(400, "invalid_replication", f"audio_policy must be one of {', '.join(AUDIO_POLICIES)}")
    media = source.get("media") if isinstance(source.get("media"), dict) else {}
    if audio_policy in {"copy-source", "reference-source"} and media.get("has_audio") is not True:
        raise ApiError(422, "audio_stream_missing", f"audio_policy {audio_policy!r} requires source audio")
    source_frames = _source_frame_count(media)
    cut_frames = _normalize_cut_frames(data.get("cut_frames"), source_frames)
    aspect_ratio, width, height = _output_dimensions(media)
    expert_prompt = _text(data.get("prompt", ""), "prompt", maximum=MAX_PROMPT_LENGTH, preserve=True)
    prompt = build_prompt(
        brief=brief,
        preserve=preserve,
        replace=replace,
        references=[{"asset_id": item["asset_id"], "role": item["role"]} for item in recipe_references],
        expert_prompt=expert_prompt,
    )
    # Bound planning work by the configured project payload budget, not seconds.
    # Every segment repeats the prompt; even its JSON string is a lower bound.
    segment_count = max(1, (source_frames + H3_MAX_FRAMES - 1) // H3_MAX_FRAMES)
    minimum_segment_bytes = len(json.dumps(prompt, ensure_ascii=False).encode("utf-8")) + 256
    if segment_count > max_project_bytes // minimum_segment_bytes:
        raise ApiError(413, "replication_project_size", "replication plan exceeds the configured project JSON budget; increase H3_STUDIO_MAX_PROJECT_JSON_BYTES or split the source")
    overlap = 22 if effective_continuity == "motion_context" else 0
    windows = _segment_windows(source_frames, cut_frames, overlap)
    source_sha = source.get("sha256")
    if not isinstance(source_sha, str) or re.fullmatch(r"[0-9a-f]{64}", source_sha) is None:
        raise ApiError(409, "source_integrity", "source asset has no persisted SHA-256")
    composed_frames = sum(item[2] for item in windows) - overlap * (len(windows) - 1)
    recipe = {
        "type": RECIPE_TYPE,
        "version": SCHEMA_VERSION,
        "source_asset_id": source_id,
        "source_sha256": source_sha,
        "brief": brief,
        "preserve": preserve,
        "replace": replace,
        "references": recipe_references,
        "prompt_policy": {"mode": "expert" if expert_prompt else "deterministic-v1", "prompt_sha256": _sha256(prompt)},
        "segmentation": {
            "fps": 24,
            "source_frames": source_frames,
            "windows": [
                {"start_frame": start, "end_frame": end, "generated_frames": generated}
                for start, end, generated in windows
            ],
            "composed_frames": composed_frames,
            "final_trim_frames": composed_frames - source_frames,
            "motion_context_frames": overlap,
        },
        "continuity": effective_continuity,
        "audio_policy": audio_policy,
        "output": {
            "aspect_ratio": aspect_ratio, "width": width, "height": height,
            "frames": source_frames, "duration": source_frames / 24,
        },
    }
    validate_recipe(recipe)
    segments: list[dict[str, Any]] = []
    for index, (start, end, generated) in enumerate(windows):
        parameters: dict[str, Any] = {
            "duration": generated / 24,
            "width": width,
            "height": height,
            "steps": steps,
            "seed": seed,
            "denoise": 1.0,
            "ref_image_size": "match",
        }
        if profile.sampling_mode == "turbo4":
            parameters["lora_strength"] = lora_strength
        continuation = "none" if index == 0 else effective_continuity
        segment: dict[str, Any] = {
            "continuation": continuation,
            "request": {
                "prompt": prompt,
                "prompt_mode": "preserve_tags_only",
                "director_mode": "rv2v",
                "parameters": parameters,
                "profile_id": profile.id,
                "profile_version": profile.version,
                "profile_digest": profile.digest(),
                "references": references,
            },
            "source_range": {"asset_id": source_id, "start_frame": start - (overlap if index else 0), "end_frame": end, "fps": 24.0},
        }
        if continuation == "motion_context":
            segment["motion_context"] = {"video_frames": 22, "audio_frames": 24}
        segments.append(segment)
    project = {
        "title": title,
        "recipe": recipe,
        "storyboard": {
            "source_asset_id": source_id,
            "fps": 24.0,
            "frame_count": source_frames,
            "cut_frames": [end for _, end, _ in windows[:-1]],
        },
        "segments": segments,
    }
    if len(json.dumps(project, ensure_ascii=False).encode("utf-8")) > max_project_bytes:
        raise ApiError(413, "replication_project_size", "replication plan exceeds the configured project JSON budget; increase H3_STUDIO_MAX_PROJECT_JSON_BYTES or split the source")
    return {
        "version": SCHEMA_VERSION,
        "recipe": recipe,
        "profile": {
            "id": profile.id, "version": profile.version,
            "manifest_sha256": profile.digest(), "sampling_mode": profile.sampling_mode,
        },
        "prompt": prompt,
        "summary": {
            "source_duration": source_frames / 24,
            "output_duration": source_frames / 24,
            "segment_count": len(segments),
            "final_trim_frames": composed_frames - source_frames,
            "continuity": effective_continuity,
            "audio_policy": audio_policy,
        },
        "project": project,
    }
