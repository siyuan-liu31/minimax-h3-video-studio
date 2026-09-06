"""Deterministic planning for durable H3 character-migration projects."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from .errors import ApiError
from .profiles import H3_MAX_FRAMES, ProfileRegistry, WorkflowProfile
from .security import validate_id


SCHEMA_VERSION = "h3.character-migration/v1"
RECIPE_TYPE = "character_migration"
SUPPORTED_OVERLAPS = (5, 22, 39, 56)
AUDIO_POLICIES = ("copy-source", "reference-source", "generate", "mute")
LEGAL_SEGMENT_FRAMES = tuple(range(124, H3_MAX_FRAMES + 1, 17))
DEFAULT_SEGMENT_FRAMES = 243
DEFAULT_OVERLAP_FRAMES = 39
MAX_SUBJECT_LENGTH = 500
MAX_DETAILS_LENGTH = 4_000
MAX_PROMPT_LENGTH = 12_000


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
    available = bool(motion_context.get("available")) and any(item["available"] for item in supported)
    return {
        "available": available,
        "recipe_version": SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "supported_profiles": supported,
        "segment_frames": list(LEGAL_SEGMENT_FRAMES),
        "overlap_frames": list(SUPPORTED_OVERLAPS),
        "audio_policies": list(AUDIO_POLICIES),
        "limits": {"targets": 1, "fps": 24, "max_segment_frames": H3_MAX_FRAMES},
        "motion_context": {
            "required": True,
            "available": motion_context.get("available") is True,
        },
    }


def _text(
    value: Any,
    field: str,
    *,
    maximum: int,
    required: bool = False,
    preserve: bool = False,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, "invalid_character_migration", f"{field} must be a string")
    result = value if preserve else value.strip()
    if required and not result.strip():
        raise ApiError(400, "invalid_character_migration", f"{field} must not be empty")
    if len(result) > maximum:
        raise ApiError(400, "invalid_character_migration", f"{field} must contain at most {maximum} characters")
    return result


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_character_migration", f"{field} must be an integer")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ApiError(400, "invalid_character_migration", f"{field} must be a finite number")
    return float(value)


def _profile_number(profile: WorkflowProfile, key: str, value: float) -> None:
    limits = profile.limits.get(key)
    if not isinstance(limits, (list, tuple)) or len(limits) != 2:
        return
    minimum, maximum = float(limits[0]), float(limits[1])
    if value < minimum or value > maximum:
        raise ApiError(
            400,
            "invalid_character_migration",
            f"{key} received {value:g}; allowed range for profile {profile.id} is {minimum:g}..{maximum:g}",
        )


def _display_dimensions(media: dict[str, Any]) -> tuple[int, int]:
    width = int(media.get("width", 0) or 0)
    height = int(media.get("height", 0) or 0)
    rotation = int(media.get("rotation", 0) or 0) % 360
    if rotation in {90, 270}:
        width, height = height, width
    if width <= 0 or height <= 0:
        raise ApiError(422, "source_dimensions_missing", "source video display dimensions are unavailable; re-upload a readable video")
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
    frame_count = media.get("frame_count")
    reference_fps = media.get("reference_fps", media.get("fps"))
    if (
        isinstance(frame_count, int) and not isinstance(frame_count, bool) and frame_count > 0
        and isinstance(reference_fps, (int, float))
        and math.isclose(float(reference_fps), 24.0, abs_tol=0.01)
    ):
        return frame_count
    duration = _number(media.get("video_duration") or media.get("duration"), "source duration")
    if duration <= 0:
        raise ApiError(422, "source_duration_missing", "source video duration must be positive")
    # Character migration always plans on the normalized 24 fps timeline.
    frames = int(round(duration * 24.0))
    if frames <= 0:
        raise ApiError(422, "source_duration_missing", "source video has no frames on the 24 fps planning timeline")
    return frames


def _validate_subject(value: Any) -> str:
    subject = _text(value, "targets[0].source_subject", maximum=MAX_SUBJECT_LENGTH, required=True)
    if len(subject) < 3 or subject.casefold() in {"person", "someone", "subject", "the person", "a person", "人", "人物"}:
        raise ApiError(
            400,
            "ambiguous_source_subject",
            "targets[0].source_subject must identify one person by position, clothing, role, or another visible distinction",
        )
    return subject


def build_prompt(*, source_subject: str, character_asset_id: str, details: str = "", expert_prompt: str = "") -> str:
    if expert_prompt:
        return expert_prompt
    target = f"@{{{character_asset_id}}}"
    clauses = [
        f"In the source video reference, bind <Subject 1> to {source_subject}.",
        f"Bind <Subject 2> to {target}.",
        "<Subject 2> fully replaces <Subject 1> for the entire clip.",
        "<Subject 1>: identity_not_preserved.",
        f"{target}: fully_referenced.",
        "<Subject 2>: identity_fully_preserved.",
        "Preserve from the source video motion, pose, timing, position, camera movement, framing, environment, lighting, composition, and interactions.",
        "Exclude the original identity, identity leakage, identity blending, morphing, duplicate people, temporal flicker, and unintended wardrobe drift.",
    ]
    if details:
        clauses.append(details.rstrip(". ") + ".")
    return " ".join(clauses)


def _estimate_storage(
    *, width: int, height: int, source_frames: int, generated_frames: int,
    segment_frames: int, overlap_frames_total: int, source_size: int,
) -> dict[str, int]:
    # Conservative compressed-video estimate with explicit merge/finalization
    # staging and disk-backed latent-tail allowance. It is deterministic and
    # intentionally independent of machine RAM/VRAM.
    output_bytes = max(source_size, int(width * height * generated_frames * 0.10))
    final_bytes = max(int(source_size * 1.25), int(width * height * source_frames * 0.10))
    contexts = int(width * height * overlap_frames_total * 2 / 8)
    derivative_peak = max(
        8 * 1024 * 1024,
        int(source_size * min(1.0, segment_frames / max(1, source_frames))),
    )
    required = output_bytes + final_bytes * 2 + contexts + derivative_peak
    return {
        "segment_outputs_bytes": output_bytes,
        "final_output_bytes": final_bytes,
        "motion_context_bytes": contexts,
        "source_derivative_peak_bytes": derivative_peak,
        "required_free_bytes": required,
    }


def validate_recipe(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_character_migration_recipe", "recipe must be an object")
    allowed = {
        "type", "version", "source_asset_id", "source_sha256", "targets",
        "prompt_policy", "segmentation", "audio_policy", "output",
    }
    extra = set(value) - allowed
    if extra:
        raise ApiError(400, "invalid_character_migration_recipe", f"unsupported recipe field: {sorted(extra)[0]}")
    if value.get("type") != RECIPE_TYPE or value.get("version") != SCHEMA_VERSION:
        raise ApiError(400, "invalid_character_migration_recipe", f"recipe type/version must be {RECIPE_TYPE}/{SCHEMA_VERSION}")
    source_asset_id = validate_id(value.get("source_asset_id"), "recipe source_asset_id")
    source_sha256 = value.get("source_sha256")
    if not isinstance(source_sha256, str) or len(source_sha256) != 64 or any(char not in "0123456789abcdef" for char in source_sha256):
        raise ApiError(400, "invalid_character_migration_recipe", "recipe source_sha256 must be 64 lowercase hex characters")
    targets = value.get("targets")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        raise ApiError(400, "invalid_character_migration_recipe", "recipe targets must contain exactly one target in v1")
    target_allowed = {"character_asset_id", "character_sha256", "source_subject", "details"}
    if set(targets[0]) - target_allowed:
        raise ApiError(400, "invalid_character_migration_recipe", "recipe target has unsupported fields")
    validate_id(targets[0].get("character_asset_id"), "recipe character_asset_id")
    character_sha = targets[0].get("character_sha256")
    if not isinstance(character_sha, str) or re.fullmatch(r"[0-9a-f]{64}", character_sha) is None:
        raise ApiError(400, "invalid_character_migration_recipe", "recipe character_sha256 must be 64 lowercase hex characters")
    _validate_subject(targets[0].get("source_subject"))
    if "details" in targets[0]:
        _text(targets[0]["details"], "recipe target details", maximum=MAX_DETAILS_LENGTH)
    prompt_policy = value.get("prompt_policy")
    if not isinstance(prompt_policy, dict) or set(prompt_policy) != {"mode", "prompt_sha256"}:
        raise ApiError(
            400, "invalid_character_migration_recipe",
            "recipe prompt_policy must contain only mode and prompt_sha256",
        )
    if prompt_policy.get("mode") not in {"deterministic-v1", "expert"}:
        raise ApiError(400, "invalid_character_migration_recipe", "recipe prompt_policy mode is unsupported")
    prompt_sha = prompt_policy.get("prompt_sha256")
    if not isinstance(prompt_sha, str) or re.fullmatch(r"[0-9a-f]{64}", prompt_sha) is None:
        raise ApiError(400, "invalid_character_migration_recipe", "recipe prompt_sha256 must be 64 lowercase hex characters")
    segmentation = value.get("segmentation")
    segmentation_keys = {
        "fps", "source_frames", "segment_frames", "overlap_frames", "stride_frames",
        "composed_frames", "final_trim_frames",
    }
    if (
        not isinstance(segmentation, dict)
        or not segmentation_keys.issubset(segmentation)
        or set(segmentation) - (segmentation_keys | {"terminal_overlap_frames"})
    ):
        raise ApiError(
            400, "invalid_character_migration_recipe",
            "recipe segmentation fields are incomplete or unsupported",
        )
    numbers = {
        key: _integer(segmentation.get(key), f"recipe segmentation {key}")
        for key in segmentation_keys
    }
    if numbers["fps"] != 24 or numbers["source_frames"] <= 0:
        raise ApiError(
            400, "invalid_character_migration_recipe",
            "recipe segmentation requires fps 24 and positive source_frames",
        )
    if (
        numbers["segment_frames"] not in LEGAL_SEGMENT_FRAMES
        or numbers["overlap_frames"] not in SUPPORTED_OVERLAPS
    ):
        raise ApiError(
            400, "invalid_character_migration_recipe",
            "recipe segmentation uses an unsupported segment or overlap frame count",
        )
    if numbers["stride_frames"] != numbers["segment_frames"] - numbers["overlap_frames"]:
        raise ApiError(400, "invalid_character_migration_recipe", "recipe stride_frames is inconsistent")
    terminal_overlap = _integer(
        segmentation.get("terminal_overlap_frames", numbers["overlap_frames"]),
        "recipe segmentation terminal_overlap_frames",
    )
    if (
        terminal_overlap not in SUPPORTED_OVERLAPS
        or terminal_overlap < numbers["overlap_frames"]
        or terminal_overlap >= numbers["segment_frames"]
    ):
        raise ApiError(
            400,
            "invalid_character_migration_recipe",
            "recipe terminal_overlap_frames must be a supported overlap no smaller than overlap_frames",
        )
    expected_segments = (
        1
        if numbers["source_frames"] <= numbers["segment_frames"]
        else 1 + math.ceil(
            (numbers["source_frames"] - numbers["segment_frames"])
            / numbers["stride_frames"]
        )
    )
    expected_composed = numbers["segment_frames"]
    if expected_segments > 1:
        expected_composed += (
            (expected_segments - 2) * numbers["stride_frames"]
            + numbers["segment_frames"] - terminal_overlap
        )
    if (
        numbers["composed_frames"] != expected_composed
        or numbers["final_trim_frames"] != expected_composed - numbers["source_frames"]
    ):
        raise ApiError(
            400, "invalid_character_migration_recipe",
            "recipe composed/final trim frame counts are inconsistent",
        )
    audio_policy = value.get("audio_policy")
    if audio_policy not in AUDIO_POLICIES:
        raise ApiError(400, "invalid_character_migration_recipe", f"recipe audio_policy must be one of {', '.join(AUDIO_POLICIES)}")
    output = value.get("output")
    if not isinstance(output, dict) or set(output) != {"aspect_ratio", "width", "height", "frames", "duration"}:
        raise ApiError(
            400, "invalid_character_migration_recipe",
            "recipe output fields are incomplete or unsupported",
        )
    expected_dimensions = {
        "16:9": (1344, 768), "9:16": (768, 1344), "1:1": (1024, 1024),
    }
    if output.get("aspect_ratio") not in expected_dimensions:
        raise ApiError(400, "invalid_character_migration_recipe", "recipe output aspect_ratio is unsupported")
    width = _integer(output.get("width"), "recipe output width")
    height = _integer(output.get("height"), "recipe output height")
    frames = _integer(output.get("frames"), "recipe output frames")
    duration = _number(output.get("duration"), "recipe output duration")
    if (
        (width, height) != expected_dimensions[output["aspect_ratio"]]
        or frames != numbers["source_frames"]
        or not math.isclose(duration, frames / 24, abs_tol=1e-9)
    ):
        raise ApiError(
            400, "invalid_character_migration_recipe",
            "recipe output does not match its segmentation or aspect ratio",
        )
    # JSON round-trip returns a detached, persistence-safe value.
    return json.loads(json.dumps(value))


def plan(
    data: Any,
    *,
    source: dict[str, Any],
    character: dict[str, Any],
    registry: ProfileRegistry,
    available_profiles: set[str] | None = None,
    motion_context_available: bool = True,
    free_disk_bytes: int | None = None,
    merged_quota_bytes: int | None = None,
    motion_context_quota_bytes: int | None = None,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_character_migration", "character migration input must be an object")
    allowed = {
        "version", "source_asset_id", "targets", "profile_id", "profile_version",
        "profile_digest", "steps", "lora_strength", "seed", "segment_frames",
        "overlap_frames", "audio_policy", "prompt", "title",
    }
    extra = set(data) - allowed
    if extra:
        raise ApiError(400, "invalid_character_migration", f"unknown field: {sorted(extra)[0]}")
    if data.get("version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ApiError(400, "unsupported_character_migration_version", f"version must be {SCHEMA_VERSION}")
    source_id = validate_id(data.get("source_asset_id"), "source_asset_id")
    if source.get("id") != source_id or source.get("kind") != "video":
        raise ApiError(400, "source_media_kind", "source_asset_id must identify a readable video")
    targets = data.get("targets")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        raise ApiError(400, "invalid_character_migration", "targets must contain exactly one object in v1")
    if set(targets[0]) - {"character_asset_id", "source_subject", "details"}:
        raise ApiError(400, "invalid_character_migration", "targets[0] contains an unknown field")
    character_id = validate_id(targets[0].get("character_asset_id"), "targets[0].character_asset_id")
    if character.get("id") != character_id or character.get("kind") != "image":
        raise ApiError(400, "character_media_kind", "targets[0].character_asset_id must identify a readable image")
    source_subject = _validate_subject(targets[0].get("source_subject"))
    details = _text(targets[0].get("details", ""), "targets[0].details", maximum=MAX_DETAILS_LENGTH)
    expert_prompt = _text(
        data.get("prompt", ""), "prompt", maximum=MAX_PROMPT_LENGTH, preserve=True,
    )
    profile_id = _text(data.get("profile_id", "minimax-h3-ref2va"), "profile_id", maximum=200, required=True)
    profile = registry.get(profile_id)
    if profile.output_type != "video" or profile.compiler != "h3_ref":
        raise ApiError(400, "character_migration_profile", "profile_id must select an H3 Ref2VA Base or Turbo profile")
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
        raise ApiError(503, "profile_unavailable", f"profile_id {profile.id!r} is unavailable; install its required models and nodes")
    if not motion_context_available:
        raise ApiError(503, "motion_context_unavailable", "character migration requires the configured Motion Context nodes")
    steps = _integer(data.get("steps", int(profile.defaults.get("steps", 4))), "steps")
    _profile_number(profile, "steps", float(steps))
    lora_strength = _number(data.get("lora_strength", profile.defaults.get("lora_strength", 0)), "lora_strength")
    if profile.sampling_mode != "turbo4" and not math.isclose(lora_strength, 0.0):
        raise ApiError(400, "invalid_character_migration", "lora_strength must be 0 for a Base profile")
    _profile_number(profile, "lora_strength", lora_strength)
    seed = _integer(data.get("seed", -1), "seed")
    if seed < -1:
        raise ApiError(400, "invalid_character_migration", "seed must be -1 or a non-negative integer")
    segment_frames = _integer(data.get("segment_frames", DEFAULT_SEGMENT_FRAMES), "segment_frames")
    if segment_frames not in LEGAL_SEGMENT_FRAMES:
        raise ApiError(400, "invalid_character_migration", f"segment_frames received {segment_frames}; allowed values are 17k+5 from 124 through {H3_MAX_FRAMES}")
    overlap = _integer(data.get("overlap_frames", DEFAULT_OVERLAP_FRAMES), "overlap_frames")
    if overlap not in SUPPORTED_OVERLAPS:
        raise ApiError(400, "invalid_character_migration", f"overlap_frames received {overlap}; allowed values are {list(SUPPORTED_OVERLAPS)}")
    if overlap >= segment_frames:
        raise ApiError(400, "invalid_character_migration", f"overlap_frames received {overlap}; it must be smaller than segment_frames {segment_frames}")
    audio_policy = data.get("audio_policy", "copy-source")
    if audio_policy not in AUDIO_POLICIES:
        raise ApiError(400, "invalid_character_migration", f"audio_policy must be one of {', '.join(AUDIO_POLICIES)}")
    source_media = source.get("media") if isinstance(source.get("media"), dict) else {}
    if audio_policy in {"copy-source", "reference-source"} and source_media.get("has_audio") is not True:
        raise ApiError(422, "audio_stream_missing", f"audio_policy {audio_policy!r} requires a usable source audio stream; choose generate or mute")
    source_frames = _source_frame_count(source_media)
    aspect_ratio, width, height = _output_dimensions(source_media)
    stride = segment_frames - overlap
    segment_count = 1 if source_frames <= segment_frames else 1 + math.ceil((source_frames - segment_frames) / stride)
    terminal_overlap = overlap
    if segment_count > 1:
        frames_before_terminal = segment_frames + max(0, segment_count - 2) * stride
        remaining_frames = source_frames - frames_before_terminal
        terminal_overlap = max(
            candidate
            for candidate in SUPPORTED_OVERLAPS
            if candidate >= overlap
            and candidate < segment_frames
            and segment_frames - candidate >= remaining_frames
        )
        composed_frames = frames_before_terminal + segment_frames - terminal_overlap
    else:
        composed_frames = segment_frames
    final_trim_frames = composed_frames - source_frames
    prompt = build_prompt(
        source_subject=source_subject,
        character_asset_id=character_id,
        details=details,
        expert_prompt=expert_prompt,
    )
    source_sha = source.get("sha256")
    character_sha = character.get("sha256")
    if not isinstance(source_sha, str) or len(source_sha) != 64:
        raise ApiError(409, "source_integrity", "source asset has no persisted SHA-256; re-upload it")
    if not isinstance(character_sha, str) or len(character_sha) != 64:
        raise ApiError(409, "character_integrity", "character asset has no persisted SHA-256; re-upload it")
    title = _text(data.get("title", "Character migration"), "title", maximum=200, required=True)
    recipe = {
        "type": RECIPE_TYPE,
        "version": SCHEMA_VERSION,
        "source_asset_id": source_id,
        "source_sha256": source_sha,
        "targets": [{
            "character_asset_id": character_id,
            "character_sha256": character_sha,
            "source_subject": source_subject,
            **({"details": details} if details else {}),
        }],
        "prompt_policy": {"mode": "expert" if expert_prompt else "deterministic-v1", "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()},
        "segmentation": {
            "fps": 24, "source_frames": source_frames, "segment_frames": segment_frames,
            "overlap_frames": overlap, "stride_frames": stride,
            **({"terminal_overlap_frames": terminal_overlap} if segment_count > 1 else {}),
            "composed_frames": composed_frames, "final_trim_frames": final_trim_frames,
        },
        "audio_policy": audio_policy,
        "output": {"aspect_ratio": aspect_ratio, "width": width, "height": height, "frames": source_frames, "duration": source_frames / 24},
    }
    segments: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    output_cursor = 0
    for index in range(segment_count):
        segment_overlap = 0 if index == 0 else (
            terminal_overlap if index == segment_count - 1 else overlap
        )
        start = 0 if index == 0 else output_cursor - segment_overlap
        end = min(source_frames, start + segment_frames)
        valid_frames = end - start
        parameters: dict[str, Any] = {
            "duration": segment_frames / 24,
            "width": width,
            "height": height,
            "steps": steps,
            "seed": seed,
            "denoise": 1.0,
            "ref_image_size": "match",
        }
        if profile.sampling_mode == "turbo4":
            parameters["lora_strength"] = lora_strength
        segment = {
            "continuation": "none" if index == 0 else "motion_context",
            "request": {
                "prompt": prompt,
                "prompt_mode": "preserve_tags_only",
                "director_mode": "rv2v",
                "parameters": parameters,
                "profile_id": profile.id,
                "profile_version": profile.version,
                "profile_digest": profile.digest(),
                "references": [{"asset_id": character_id, "role": "identity"}],
            },
            "source_range": {"asset_id": source_id, "start_frame": start, "end_frame": end, "fps": 24.0},
        }
        if index:
            segment["motion_context"] = {
                "video_frames": segment_overlap, "audio_frames": segment_overlap,
            }
        segments.append(segment)
        windows.append({
            "index": index, "source_start_frame": start, "source_end_frame": end,
            "source_frames": valid_frames, "generated_frames": segment_frames,
            "input_padding_frames": segment_frames - valid_frames,
            "trim_head_frames": segment_overlap,
            "owned_output_frames": segment_frames - segment_overlap,
            "source_video_tag": "<Video 1>", "target_picture_tag": "<Picture 1>",
        })
        output_cursor += segment_frames - segment_overlap
    estimate = _estimate_storage(
        width=width, height=height, source_frames=source_frames,
        generated_frames=composed_frames, segment_frames=segment_frames,
        overlap_frames_total=sum(
            int(item["trim_head_frames"]) for item in windows[1:]
        ),
        source_size=int(source.get("storage_size", source.get("size", 0)) or 0),
    )
    if merged_quota_bytes is not None and estimate["final_output_bytes"] > merged_quota_bytes:
        raise ApiError(507, "merge_quota", "estimated final output exceeds H3_STUDIO_MAX_MERGED_OUTPUT_BYTES; increase the quota or use a shorter source")
    if motion_context_quota_bytes is not None and estimate["motion_context_bytes"] > motion_context_quota_bytes:
        raise ApiError(507, "motion_context_storage_full", "estimated Motion Context storage exceeds its configured quota")
    if free_disk_bytes is not None and estimate["required_free_bytes"] > free_disk_bytes:
        raise ApiError(507, "disk_full", f"estimated required storage is {estimate['required_free_bytes']} bytes but only {free_disk_bytes} bytes are free")
    project = {"title": title, "recipe": recipe, "storyboard": {"source_asset_id": source_id, "fps": 24.0, "frame_count": source_frames, "cut_frames": [int(item["source_start_frame"]) for item in windows[1:]]}, "segments": segments}
    return {
        "version": SCHEMA_VERSION,
        "recipe": recipe,
        "profile": {"id": profile.id, "version": profile.version, "manifest_sha256": profile.digest(), "sampling_mode": profile.sampling_mode},
        "windows": windows,
        "final_trim": {"start_frame": 0, "end_frame": source_frames, "remove_tail_frames": final_trim_frames, "fps": 24},
        "prompt": prompt,
        "storage_estimate": estimate,
        "project": project,
    }
