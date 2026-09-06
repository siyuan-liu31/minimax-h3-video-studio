"""Strict, side-effect-free validation for long-video acceptance manifests."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.mp4$")
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
PART_KEYS = {"subject", "action", "scene", "camera", "light", "style", "dialogue", "sound", "music"}
PARAMETER_KEYS = {"aspect_ratio", "duration", "steps", "lora_strength", "seed", "mode", "ref_image_size", "denoise"}
REFERENCE_KEYS = {"id", "asset_id", "role", "include_audio", "voice_speaker", "voice_subject"}
REFERENCE_ROLES = {
    "first_frame", "last_frame", "identity", "style", "composition", "reference",
    "motion", "camera", "pacing", "voice", "music", "rhythm",
}
CONTINUATIONS = {"none", "tail_frame", "previous_video"}
H3_FPS = 24
H3_MAX_FRAMES = 362
H3_MAX_DURATION = H3_MAX_FRAMES / H3_FPS


def _object(value: Any, label: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} has unsupported fields: {', '.join(sorted(unknown))}")
    return value


def _text(value: Any, label: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > maximum or "\x00" in value:
        qualifier = "non-empty " if required else ""
        raise ValueError(f"{label} must be {qualifier}text no longer than {maximum} characters without NUL bytes")
    return value.strip()


def _number(value: Any, label: str, minimum: float, maximum: float, *, integer: bool = False) -> int | float:
    valid = isinstance(value, int if integer else (int, float)) and not isinstance(value, bool)
    if not valid or not minimum <= value <= maximum:
        kind = "integer" if integer else "number"
        raise ValueError(f"{label} must be a {kind} between {minimum:g} and {maximum:g}")
    return int(value) if integer else float(value)


def _validate_reference(value: Any, label: str) -> dict[str, Any]:
    reference = _object(value, label, REFERENCE_KEYS)
    supplied = [key for key in ("id", "asset_id") if key in reference]
    if len(supplied) != 1 or OPAQUE_ID.fullmatch(str(reference.get(supplied[0], ""))) is None:
        raise ValueError(f"{label} must contain exactly one valid id or asset_id")
    role = reference.get("role", "reference")
    if role not in REFERENCE_ROLES:
        raise ValueError(f"{label} has unsupported role {role!r}")
    if "include_audio" in reference and not isinstance(reference["include_audio"], bool):
        raise ValueError(f"{label}.include_audio must be boolean")
    speaker = reference.get("voice_speaker")
    subject = reference.get("voice_subject")
    if speaker is not None and (not isinstance(speaker, str) or re.fullmatch(r"S[1-9]\d*", speaker.upper()) is None):
        raise ValueError(f"{label}.voice_speaker must look like S1")
    if subject is not None:
        _number(subject, f"{label}.voice_subject", 1, 9999, integer=True)
    if role == "voice" and (speaker is None or subject is None):
        raise ValueError(f"{label} voice references require voice_speaker and voice_subject")
    asset_id = str(reference[supplied[0]])
    normalized: dict[str, Any] = {"asset_id": asset_id, "role": role}
    for key in ("include_audio", "voice_speaker", "voice_subject"):
        if key in reference:
            normalized[key] = deepcopy(reference[key])
    return normalized


def _validate_request(value: Any, label: str) -> dict[str, Any]:
    request = _object(
        value, label,
        {"prompt", "parts", "parameters", "profile_id", "profile_version", "profile_digest", "references"},
    )
    prompt = _text(request.get("prompt", ""), f"{label}.prompt", 12_000, required=False)
    parts_raw = request.get("parts", {})
    parts = _object(parts_raw, f"{label}.parts", PART_KEYS)
    normalized_parts: dict[str, str] = {}
    for key, raw in parts.items():
        normalized_parts[key] = _text(raw, f"{label}.parts.{key}", 4_000, required=False)
    if not prompt and not any(normalized_parts.values()):
        raise ValueError(f"{label} requires prompt or at least one non-empty prompt part")

    parameters = _object(request.get("parameters"), f"{label}.parameters", PARAMETER_KEYS)
    if parameters.get("aspect_ratio") not in {"16:9", "9:16"}:
        raise ValueError(f"{label}.parameters.aspect_ratio must be 16:9 or 9:16")
    normalized_parameters = deepcopy(parameters)
    normalized_parameters["duration"] = _number(
        parameters.get("duration"), f"{label}.parameters.duration", 5, H3_MAX_DURATION,
    )
    normalized_parameters["steps"] = _number(parameters.get("steps"), f"{label}.parameters.steps", 4, 50, integer=True)
    normalized_parameters["seed"] = _number(parameters.get("seed"), f"{label}.parameters.seed", -1, 2**63 - 1, integer=True)
    if "lora_strength" in parameters:
        normalized_parameters["lora_strength"] = _number(parameters["lora_strength"], f"{label}.parameters.lora_strength", 0, 2)
    if "denoise" in parameters:
        normalized_parameters["denoise"] = _number(parameters["denoise"], f"{label}.parameters.denoise", 0.05, 1)
    if "mode" in parameters and parameters["mode"] not in {"auto", "text", "fl2va", "ref2va"}:
        raise ValueError(f"{label}.parameters.mode is invalid")
    if "ref_image_size" in parameters and parameters["ref_image_size"] not in {"match", "max"}:
        raise ValueError(f"{label}.parameters.ref_image_size must be match or max")

    profile_id = _text(request.get("profile_id"), f"{label}.profile_id", 200)
    profile_version = _text(request.get("profile_version"), f"{label}.profile_version", 200)
    digest = request.get("profile_digest")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise ValueError(f"{label}.profile_digest must be 64 lowercase hexadecimal characters")
    references_raw = request.get("references", [])
    if not isinstance(references_raw, list) or len(references_raw) > 12:
        raise ValueError(f"{label}.references must be an array of at most 12 items")
    references = [_validate_reference(item, f"{label}.references[{index}]") for index, item in enumerate(references_raw)]
    identities = [item.get("id", item.get("asset_id")) for item in references]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label}.references contains duplicate asset ids")
    return {
        "prompt": prompt,
        **({"parts": normalized_parts} if normalized_parts else {}),
        "parameters": normalized_parameters,
        "profile_id": profile_id,
        "profile_version": profile_version,
        "profile_digest": digest,
        "references": references,
    }


def validate_manifest(value: Any) -> dict[str, Any]:
    manifest = _object(value, "manifest", {"version", "project", "rerun", "acceptance"})
    if type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("manifest.version must be the integer 1")
    project = _object(manifest.get("project"), "manifest.project", {"title", "segments"})
    title = _text(project.get("title"), "manifest.project.title", 200)
    raw_segments = project.get("segments")
    if not isinstance(raw_segments, list) or not 3 <= len(raw_segments) <= 1000:
        raise ValueError("manifest.project.segments must contain between 3 and 1000 segments")
    segments: list[dict[str, Any]] = []
    for index, value in enumerate(raw_segments):
        segment = _object(value, f"segment {index}", {"id", "continuation", "request"})
        continuation = segment.get("continuation")
        if continuation not in CONTINUATIONS:
            raise ValueError(f"segment {index}.continuation is invalid")
        normalized: dict[str, Any] = {
            "continuation": continuation,
            "request": _validate_request(segment.get("request"), f"segment {index}.request"),
        }
        if continuation != "none" and len(normalized["request"]["references"]) > 5:
            raise ValueError(f"segment {index} continuation reserves one of the 6 reference slots")
        if "id" in segment:
            if not isinstance(segment["id"], str) or OPAQUE_ID.fullmatch(segment["id"]) is None:
                raise ValueError(f"segment {index}.id must be 32 lowercase hexadecimal characters")
            normalized["id"] = segment["id"]
        segments.append(normalized)
    if [segment["continuation"] for segment in segments[:3]] != ["none", "tail_frame", "previous_video"]:
        raise ValueError("the first three continuation modes must be none, tail_frame, previous_video")
    if any(segment["continuation"] == "none" for segment in segments[1:]):
        raise ValueError("only the first segment may use continuation=none")
    ratios = {segment["request"]["parameters"]["aspect_ratio"] for segment in segments}
    if len(ratios) != 1:
        raise ValueError("all segments must use the same aspect_ratio for deterministic merging")

    rerun_raw = manifest.get("rerun")
    rerun: dict[str, Any] | None = None
    if rerun_raw is not None:
        source = _object(rerun_raw, "manifest.rerun", {"segment_index", "prompt", "seed"})
        index = _number(source.get("segment_index"), "manifest.rerun.segment_index", 0, len(segments) - 1, integer=True)
        prompt = _text(source.get("prompt"), "manifest.rerun.prompt", 12_000)
        seed = _number(source.get("seed"), "manifest.rerun.seed", -1, 2**63 - 1, integer=True)
        original = segments[index]["request"]
        if prompt == original["prompt"] and seed == original["parameters"]["seed"]:
            raise ValueError("manifest.rerun must change the prompt or seed")
        rerun = {"segment_index": index, "prompt": prompt, "seed": seed}

    acceptance = _object(
        manifest.get("acceptance"), "manifest.acceptance",
        {"width", "height", "expect_audio", "duration_tolerance", "output_name"},
    )
    width = _number(acceptance.get("width"), "manifest.acceptance.width", 256, 2048, integer=True)
    height = _number(acceptance.get("height"), "manifest.acceptance.height", 256, 2048, integer=True)
    expected = (1344, 768) if ratios == {"16:9"} else (768, 1344)
    if (width, height) != expected:
        raise ValueError(f"acceptance dimensions must match the H3 preset {expected[0]}x{expected[1]}")
    expect_audio = acceptance.get("expect_audio", True)
    if expect_audio is not True:
        raise ValueError("manifest.acceptance.expect_audio must be true for H3 merge acceptance")
    tolerance = _number(acceptance.get("duration_tolerance", 0.25), "manifest.acceptance.duration_tolerance", 0.04, 2)
    output_name = acceptance.get("output_name", "merged.mp4")
    if not isinstance(output_name, str) or SAFE_OUTPUT_NAME.fullmatch(output_name) is None or Path(output_name).name != output_name:
        raise ValueError("manifest.acceptance.output_name must be a safe .mp4 basename")
    result = {
        "version": 1,
        "project": {"title": title, "segments": segments},
        "acceptance": {
            "width": width, "height": height, "expect_audio": expect_audio,
            "duration_tolerance": tolerance, "output_name": output_name,
        },
    }
    if rerun is not None:
        result["rerun"] = rerun
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read manifest {path}: {error}") from error
    return validate_manifest(value)
