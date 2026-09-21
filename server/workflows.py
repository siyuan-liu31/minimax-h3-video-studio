"""Validated canvas requests and ComfyUI API workflow compilation."""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable

from .config import Config
from .errors import ApiError, CapabilityError
from .prompting import compile_prompt, replace_reference_tokens
from .security import validate_id
from .profiles import (
    DEFAULT_REGISTRY,
    H3_MAX_DURATION_SECONDS,
    H3_MAX_FRAMES,
    H3_MAX_REFERENCES,
    H3_REFERENCE_CAPACITY,
    ProfileRegistry,
    WorkflowProfile,
)


H3_PRESETS: dict[str, tuple[int, int]] = {
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "1:1": (1024, 1024),
}
IMAGE_PRESETS: dict[str, tuple[int, int]] = {
    "16:9": (1024, 576),
    "9:16": (576, 1024),
    "3:4": (768, 1024),
    "1:1": (1024, 1024),
}
QWEN_IMAGE_21_PRESETS: dict[str, tuple[int, int]] = {
    "16:9": (2752, 1536),
    "9:16": (1536, 2752),
    "3:4": (1792, 2400),
    "1:1": (2048, 2048),
}
PROMPT_MODES = {"default", "preserve_tags_only"}
DIRECTOR_MODES = {"auto", "t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v"}
TAG_PATTERN = re.compile(r"<(Picture|Video|Audio)\s+(\d+)>", re.IGNORECASE)
REFERENCE_TOKEN_PATTERN = re.compile(r"@\{([^{}]+)\}")
ROLE_ALIASES = {"first": "first_frame", "last": "last_frame"}
VIDEO_ROLES = {
    "image": {"first_frame", "last_frame", "identity", "style", "composition", "reference"},
    "video": {"motion", "camera", "pacing", "reference"},
    "audio": {"voice", "music", "rhythm", "reference"},
}
IMAGE_ROLES = {"init_image", "image_edit", "reference"}


@dataclass(frozen=True, slots=True)
class AssetRef:
    asset_id: str
    kind: str
    comfy_path: str
    role: str = "reference"
    label: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
    include_audio: bool = False
    duration: float = 0.0
    has_audio: bool = False
    fps: float = 0.0
    voice_speaker: str = ""
    voice_subject: int = 0


@dataclass(frozen=True, slots=True)
class GenerationSpec:
    output_type: str
    prompt: str
    negative_prompt: str
    width: int
    height: int
    steps: int
    seed: int
    references: tuple[AssetRef, ...]
    mode: str
    prompt_mode: str = "default"
    duration: float = 0.0
    frames: int = 0
    lora_strength: float = 0.0
    ref_image_size: str = "match"
    cfg: float = 7.0
    denoise: float = 1.0
    profile_id: str = ""
    profile_version: str = ""
    profile_digest: str = ""
    compiler: str = ""
    sampling_mode: str = "default"
    sampler: str = ""
    scheduler: str = ""
    image_lora: str = ""
    model_bindings: dict[str, str] = field(default_factory=dict)
    reference_duration_total: float = 0.0
    director_mode: str = ""
    requested_director_mode: str = "auto"
    source_asset_id: str = ""

    def public_parameters(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "output_type": self.output_type,
            "mode": self.mode,
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "seed": self.seed,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_digest": self.profile_digest,
            "sampling_mode": self.sampling_mode,
            "sampler": self.sampler,
            "scheduler": self.scheduler,
        }
        if self.prompt_mode != "default":
            value["prompt_mode"] = self.prompt_mode
        if self.output_type == "video":
            value.update(
                {
                    "director_mode": self.director_mode,
                    "resolved_director_mode": self.director_mode,
                    "requested_director_mode": self.requested_director_mode,
                    "source_asset_id": self.source_asset_id or None,
                    "duration_requested": self.duration,
                    "frames": self.frames,
                    "duration_actual": round(self.frames / 24, 3),
                    "fps": 24,
                    "lora_strength": self.lora_strength,
                    "denoise": self.denoise,
                    "ref_image_size": self.ref_image_size,
                    "reference_duration_total": round(self.reference_duration_total, 3),
                }
            )
        else:
            value["cfg"] = self.cfg
            if self.compiler in {"z_image_lora_t2i", "z_image_lora_img2img"}:
                value["image_lora"] = self.image_lora
                value["lora_strength"] = self.lora_strength
            if self.compiler not in {"flux2_klein", "qwen_image_21"}:
                value["denoise"] = self.denoise
        return value


@dataclass(frozen=True, slots=True)
class ResumeSamplingPlan:
    mode: str
    max_total_steps: int
    steps_before: int = 0
    additional_steps: int = 0
    checkpoint_input: str = ""

    def __post_init__(self) -> None:
        if self.mode not in {"initial", "resume"}:
            raise ValueError("resume sampling mode must be initial or resume")
        if self.max_total_steps <= 0:
            raise ValueError("max_total_steps must be positive")
        if self.mode == "resume" and (
            self.steps_before <= 0 or self.additional_steps <= 0
            or self.steps_before + self.additional_steps > self.max_total_steps
            or not self.checkpoint_input
        ):
            raise ValueError("resume sampling plan is incomplete")


@dataclass(frozen=True, slots=True)
class MotionContextPlan:
    """Per-render chain state; sampling steps remain owned by GenerationSpec."""

    checkpoint_input: str = ""
    save_context: bool = False
    video_frames: int = 22
    audio_frames: int = 24

    def __post_init__(self) -> None:
        if self.video_frames not in {5, 22, 39, 56}:
            raise ValueError("Motion Context video_frames must be 5, 22, 39, or 56")
        if isinstance(self.audio_frames, bool) or not 0 <= self.audio_frames <= 240:
            raise ValueError("Motion Context audio_frames must be from 0 through 240")


def h3_frame_count(duration_seconds: float) -> int:
    """Snap to H3's 17k+5 grid without exceeding the supported output limit."""
    candidate = 5 + 17 * max(0, math.ceil((duration_seconds * 24 - 5) / 17))
    return min(candidate, H3_MAX_FRAMES)


def _validate_profile_identity(data: dict[str, Any], requested: str, profile: WorkflowProfile) -> None:
    if not requested or requested == "auto":
        return
    requested_version = str(data.get("profile_version", "")).strip()
    requested_digest = str(data.get("profile_digest", "")).strip()
    if not requested_version or not requested_digest:
        raise ApiError(400, "profile_identity_required", "explicit profile_id requires profile_version and profile_digest from /api/capabilities")
    if not profile.accepts_identity(requested_version, requested_digest):
        raise ApiError(409, "profile_version_mismatch", "the selected workflow profile changed; refresh capabilities")


def _number(
    value: Any,
    label: str,
    *,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> int | float:
    if isinstance(value, bool):
        raise ApiError(400, "invalid_parameter", f"{label} must be a number")
    try:
        parsed: int | float = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ApiError(400, "invalid_parameter", f"{label} must be a number") from error
    if not minimum <= parsed <= maximum:
        raise ApiError(
            400,
            "invalid_parameter",
            f"{label} must be between {minimum:g} and {maximum:g}",
        )
    return parsed


def _profile_number(
    profile: WorkflowProfile,
    parameters: dict[str, Any],
    key: str,
    fallback: float,
    *,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> int | float:
    bounds = profile.limits.get(key)
    if isinstance(bounds, list) and len(bounds) == 2 and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in bounds):
        minimum = max(minimum, float(bounds[0]))
        maximum = min(maximum, float(bounds[1]))
    if minimum > maximum:
        raise ApiError(500, "profile_invalid", f"profile {profile.id!r} has contradictory limits for {key}")
    default = profile.defaults.get(key, fallback)
    return _number(parameters.get(key, default), key, minimum=minimum, maximum=maximum, integer=integer)


def _seed(value: Any) -> int:
    if value in (None, -1, "-1"):
        return secrets.randbelow(2**63)
    return int(_number(value, "seed", minimum=0, maximum=2**63 - 1, integer=True))


def _resolution(parameters: dict[str, Any], *, image: bool, qwen_image_21: bool = False) -> tuple[int, int]:
    preset = parameters.get("aspect_ratio", parameters.get("resolution", "16:9"))
    preset_aliases = {
        "landscape": "16:9",
        "portrait": "9:16",
        "square": "1:1",
        "1344x768": "16:9",
        "768x1344": "9:16",
        "1024x1024": "1:1",
    }
    if isinstance(preset, str):
        preset = preset_aliases.get(preset.lower(), preset)
    if "width" not in parameters and "height" not in parameters:
        presets = QWEN_IMAGE_21_PRESETS if qwen_image_21 else IMAGE_PRESETS if image else H3_PRESETS
        if not isinstance(preset, str) or preset not in presets:
            raise ApiError(
                400,
                "invalid_resolution",
                f"unknown resolution preset; choose {', '.join(presets)} or provide width/height",
            )
        return presets[preset]
    if "width" not in parameters or "height" not in parameters:
        raise ApiError(400, "invalid_resolution", "width and height must be provided together")
    maximum_dimension = 3072 if qwen_image_21 else 2048
    width = int(_number(parameters["width"], "width", minimum=256, maximum=maximum_dimension, integer=True))
    height = int(_number(parameters["height"], "height", minimum=256, maximum=maximum_dimension, integer=True))
    multiple = 32 if qwen_image_21 else 8 if image else 32
    if width % multiple or height % multiple:
        raise ApiError(
            400,
            "invalid_resolution",
            f"width and height must be multiples of {multiple}",
        )
    # Image generation accepts explicit dimensions up to a 2048-square pixel
    # budget. H3 video keeps its separate, smaller reviewed resolution budget.
    maximum_pixels = 4_300_800 if qwen_image_21 else 2048 * 2048 if image else 1_179_648
    if width * height > maximum_pixels:
        raise ApiError(
            400,
            "invalid_resolution",
            f"pixel count exceeds the {'image' if image else 'H3'} safety limit",
        )
    return width, height


def _string(
    value: Any,
    label: str,
    maximum: int,
    *,
    required: bool = False,
    preserve_whitespace: bool = False,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, "invalid_parameter", f"{label} must be a string")
    if required and not value.strip():
        raise ApiError(400, "invalid_parameter", f"{label} is required")
    if len(value) > maximum:
        raise ApiError(400, "invalid_parameter", f"{label} exceeds {maximum} characters")
    return value if preserve_whitespace else value.strip()


def _node_data(node: dict[str, Any]) -> dict[str, Any]:
    data = node.get("data", {})
    return data if isinstance(data, dict) else {}


def _role(value: Any) -> str:
    role = str(value or "reference").strip().lower()
    return ROLE_ALIASES.get(role, role)


def _voice_subject(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise ApiError(400, "invalid_voice_binding", "voice Subject number must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ApiError(400, "invalid_voice_binding", "voice Subject number must be an integer") from error
    if str(parsed) != str(value).strip() and not isinstance(value, int):
        raise ApiError(400, "invalid_voice_binding", "voice Subject number must be an integer")
    return parsed


def _graph_references(
    graph: Any,
    lookup_asset: Callable[[str], dict[str, Any]],
    output_type: str,
) -> tuple[list[AssetRef], str]:
    if graph in (None, {}):
        return [], ""
    if not isinstance(graph, dict):
        raise ApiError(400, "invalid_graph", "graph must be an object")
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ApiError(400, "invalid_graph", "graph nodes and edges must be arrays")
    if len(nodes) > 64 or len(edges) > 128:
        raise ApiError(400, "invalid_graph", "graph exceeds the 64-node/128-edge limit")

    by_id: dict[str, dict[str, Any]] = {}
    generator_ids: set[str] = set()
    generic_generators: set[str] = set()
    all_generator_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise ApiError(400, "invalid_graph", "every node needs a string id")
        node_id = node["id"]
        if not node_id or len(node_id) > 128 or node_id in by_id:
            raise ApiError(400, "invalid_graph", "node ids must be unique and <= 128 chars")
        by_id[node_id] = node
        node_type = str(node.get("type", "")).lower()
        data = _node_data(node)
        declared_output = str(data.get("output_type", data.get("outputType", ""))).lower()
        if node_type in {"generate", "generator", "video-output", "image-output"}:
            all_generator_ids.add(node_id)
            if declared_output == output_type or node_type.startswith(output_type) or node_id == output_type:
                generator_ids.add(node_id)
            elif not declared_output and node_type in {"generate", "generator"}:
                generic_generators.add(node_id)

    if not generator_ids:
        if len(generic_generators) == 1:
            generator_ids = generic_generators
        elif output_type in by_id:
            generator_ids = {output_type}
        elif generic_generators:
            raise ApiError(400, "invalid_graph", "graph has ambiguous generator nodes")
    if not generator_ids:
        raise ApiError(400, "invalid_graph", f"graph has no {output_type} generator")
    if len(generator_ids) != 1:
        raise ApiError(400, "invalid_graph", f"graph must select exactly one {output_type} generator")

    # Validate the whole submitted graph, not only the browser-selected branch.
    # A restored or third-party client must not be able to bypass the canvas DAG.
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    indegree: dict[str, int] = {node_id: 0 for node_id in by_id}
    seen_edges: set[tuple[str, str]] = set()
    roles_by_source: dict[str, str] = {}
    reference_indices_by_source: dict[str, int] = {}
    connected_sources: list[str] = []
    connected_set: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ApiError(400, "invalid_graph", "every edge must be an object")
        source, target = edge.get("source"), edge.get("target")
        if source not in by_id or target not in by_id:
            raise ApiError(400, "invalid_graph", "edge references an unknown node")
        if source == target:
            raise ApiError(400, "invalid_graph", "graph cannot contain self-referencing edges")
        pair = (source, target)
        if pair in seen_edges:
            raise ApiError(400, "invalid_graph", "graph cannot contain duplicate edges")
        seen_edges.add(pair)
        source_node, target_node = by_id[source], by_id[target]
        source_type, target_type = str(source_node.get("type", "")).lower(), str(target_node.get("type", "")).lower()
        source_data = _node_data(source_node)
        source_is_asset = source_type in {"asset", "image", "video", "audio"} and (source_type == "asset" or source_data.get("assetId") or source_data.get("asset_id"))
        source_is_prompt = source_type in {"prompt", "text"}
        target_is_output = target_type == "output"
        if target in all_generator_ids:
            if not (source_is_asset or source_is_prompt):
                raise ApiError(400, "invalid_graph", "only prompt or asset nodes may feed a generator")
        elif source in all_generator_ids:
            if not target_is_output:
                raise ApiError(400, "invalid_graph", "a generator may only feed an output node")
        else:
            raise ApiError(400, "invalid_graph", "edge direction or node types are incompatible")
        adjacency[source].append(target)
        indegree[target] += 1
        if target in generator_ids:
            if source in connected_set:
                raise ApiError(400, "invalid_graph", "a source may only connect once to the selected generator")
            connected_sources.append(source)
            connected_set.add(source)
            edge_data = edge.get("data", {})
            role = edge.get("targetHandle", edge.get("role", "reference"))
            if isinstance(edge_data, dict):
                role = edge_data.get("role", role)
            roles_by_source[source] = _role(role)
            if source_is_asset and isinstance(edge_data, dict) and "reference_index" in edge_data:
                raw_index = edge_data["reference_index"]
                if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0 or raw_index >= H3_MAX_REFERENCES:
                    raise ApiError(400, "invalid_reference_order", f"reference_index must be an integer from 0 to {H3_MAX_REFERENCES - 1}")
                reference_indices_by_source[source] = raw_index

    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in adjacency[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(by_id):
        raise ApiError(400, "invalid_graph", "graph must be acyclic")

    prompt = ""
    for source in connected_sources:
        source_node = by_id[source]
        if str(source_node.get("type", "")).lower() in {"prompt", "text"}:
            candidate = _node_data(source_node).get("prompt", _node_data(source_node).get("text", ""))
            if isinstance(candidate, str) and candidate.strip():
                prompt = candidate

    reference_sources = [
        node_id for node_id in connected_sources
        if str(by_id[node_id].get("type", "")).lower() not in {"prompt", "text"}
    ]
    if reference_indices_by_source:
        if len(reference_indices_by_source) != len(reference_sources):
            raise ApiError(400, "invalid_reference_order", "every connected asset needs reference_index when ordered references are used")
        indices = list(reference_indices_by_source.values())
        if len(set(indices)) != len(indices) or set(indices) != set(range(len(indices))):
            raise ApiError(400, "invalid_reference_order", "reference_index values must be unique and contiguous from 0")
        reference_sources.sort(key=reference_indices_by_source.__getitem__)

    references: list[AssetRef] = []
    seen: set[str] = set()
    for node_id in reference_sources:
        node = by_id[node_id]
        node_type = str(node.get("type", "")).lower()
        data = _node_data(node)
        declared_kind = data.get("kind", node_type.replace("asset-", ""))
        if declared_kind not in {"image", "video", "audio"} and node_type != "asset":
            continue
        if node_type in {"prompt", "text"}:
            continue
        asset_id = data.get("assetId", data.get("asset_id"))
        if not isinstance(asset_id, str):
            raise ApiError(400, "invalid_graph", f"asset node {node_id!r} has no asset id")
        if asset_id in seen:
            raise ApiError(400, "invalid_graph", "an asset may only be connected once")
        asset = lookup_asset(asset_id)
        kind = asset.get("kind")
        if kind not in {"image", "video", "audio"}:
            raise ApiError(500, "metadata_corrupt", "asset kind is invalid")
        if declared_kind in {"image", "video", "audio"} and declared_kind != kind:
            raise ApiError(400, "invalid_graph", "node kind does not match uploaded asset")
        role = roles_by_source.get(node_id, _role(data.get("role", "reference")))
        label = str(data.get("label", asset.get("filename", "")))[:128]
        media = asset.get("media", {}) if isinstance(asset.get("media"), dict) else {}
        references.append(
            AssetRef(
                asset_id=asset_id,
                kind=kind,
                comfy_path=str(asset["comfy_path"]),
                role=role,
                label=label,
                aliases=(node_id, asset_id, label),
                include_audio=bool(data.get("include_audio", data.get("includeAudio", False))),
                duration=float(media.get("duration", 0) or 0),
                has_audio=bool(media.get("has_audio", False)),
                fps=float(media.get("reference_fps", media.get("fps", 0)) or 0),
                voice_speaker=str(data.get("voice_speaker", data.get("voiceSpeaker", ""))).upper(),
                voice_subject=_voice_subject(data.get("voice_subject", data.get("voiceSubject", 0))),
            )
        )
        seen.add(asset_id)
    return references, prompt


def _explicit_references(
    value: Any,
    lookup_asset: Callable[[str], dict[str, Any]],
) -> list[AssetRef]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ApiError(400, "invalid_references", "references must be an array")
    ordered: list[tuple[int | None, AssetRef]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            raise ApiError(400, "invalid_references", "reference must be an id or object")
        asset_id = item.get("asset_id", item.get("assetId", item.get("id")))
        if not isinstance(asset_id, str) or asset_id in seen:
            raise ApiError(400, "invalid_references", "reference asset ids must be unique")
        asset = lookup_asset(asset_id)
        kind = str(asset.get("kind", ""))
        label = str(item.get("label", asset.get("filename", "")))[:128]
        media = asset.get("media", {}) if isinstance(asset.get("media"), dict) else {}
        raw_index = item.get("reference_index", item.get("referenceIndex"))
        if raw_index is not None and (isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0 or raw_index >= H3_MAX_REFERENCES):
            raise ApiError(400, "invalid_reference_order", f"reference_index must be an integer from 0 to {H3_MAX_REFERENCES - 1}")
        ordered.append(
            (raw_index, AssetRef(
                asset_id=asset_id,
                kind=kind,
                comfy_path=str(asset.get("comfy_path", "")),
                role=_role(item.get("role", "reference")),
                label=label,
                aliases=(asset_id, label),
                include_audio=bool(item.get("include_audio", item.get("includeAudio", False))),
                duration=float(media.get("duration", 0) or 0),
                has_audio=bool(media.get("has_audio", False)),
                fps=float(media.get("reference_fps", media.get("fps", 0)) or 0),
                voice_speaker=str(item.get("voice_speaker", item.get("voiceSpeaker", ""))).upper(),
                voice_subject=_voice_subject(item.get("voice_subject", item.get("voiceSubject", 0))),
            ))
        )
        seen.add(asset_id)
    indices = [index for index, _reference in ordered if index is not None]
    if indices:
        if len(indices) != len(ordered):
            raise ApiError(400, "invalid_reference_order", "every reference needs reference_index when ordered references are used")
        if len(set(indices)) != len(indices) or set(indices) != set(range(len(indices))):
            raise ApiError(400, "invalid_reference_order", "reference_index values must be unique and contiguous from 0")
        ordered.sort(key=lambda item: int(item[0]))
    return [reference for _index, reference in ordered]


def _select_mode(requested: str, references: list[AssetRef]) -> str:
    if requested not in {"auto", "text", "fl2va", "ref2va"}:
        raise ApiError(400, "invalid_mode", "mode must be auto, text, fl2va, or ref2va")
    if requested == "text":
        if references:
            raise ApiError(400, "invalid_mode", "text mode cannot have references")
        return "text"
    if requested == "fl2va":
        if any(ref.kind != "image" for ref in references) or len(references) > 2:
            raise ApiError(400, "invalid_mode", "fl2va supports at most two image frames")
        if any(ref.role not in {"first_frame", "last_frame"} for ref in references):
            raise ApiError(400, "invalid_role", "fl2va image roles must be first_frame or last_frame")
        return "fl2va"
    if requested == "ref2va":
        if not references:
            raise ApiError(400, "invalid_mode", "ref2va requires at least one reference")
        if any(ref.role in {"first_frame", "last_frame"} for ref in references):
            raise ApiError(400, "mixed_reference_modes", "first/last frame endpoints cannot be mixed with Ref2VA references")
        return "ref2va"
    if not references:
        return "text"
    if (
        len(references) <= 2
        and all(ref.kind == "image" for ref in references)
        and all(ref.role in {"first_frame", "last_frame", "first", "last"} for ref in references)
    ):
        return "fl2va"
    if any(ref.role in {"first_frame", "last_frame"} for ref in references):
        raise ApiError(400, "mixed_reference_modes", "first/last frame endpoints cannot be mixed with identity, style, video, or audio references")
    return "ref2va"


def _director_value(
    data: dict[str, Any], parameters: dict[str, Any], key: str, default: Any,
) -> Any:
    """Read one Director field without allowing conflicting aliases."""

    direct = data.get(key, default)
    nested = parameters.get(key, default)
    if key in data and key in parameters and direct != nested:
        raise ApiError(400, "director_contract", f"conflicting {key} values")
    return direct if key in data else nested


def _resolve_director_contract(
    data: dict[str, Any],
    parameters: dict[str, Any],
    references: list[AssetRef],
    lookup_asset: Callable[[str], dict[str, Any]],
    legacy_mode: str,
) -> tuple[str, str, list[AssetRef]]:
    """Resolve a public Director preset to one trusted H3 compiler mode.

    The source is never inferred from a filename or filesystem path.  It must
    be an already-connected AssetStore video and is moved to the front before
    prompt token resolution, making it the native ``<Video 1>`` input.
    """

    requested = _director_value(data, parameters, "director_mode", "auto")
    if not isinstance(requested, str) or requested.lower() not in DIRECTOR_MODES:
        raise ApiError(
            400, "invalid_director_mode",
            "director_mode must be auto, t2v, i2v, fl2v, r2v, v2v, or rv2v",
        )
    requested = requested.lower()
    source_value = _director_value(data, parameters, "source_asset_id", None)
    source_id = ""
    if source_value not in (None, ""):
        source_id = validate_id(source_value, "source asset id")
        source = lookup_asset(source_id)
        if source.get("kind") != "video":
            raise ApiError(400, "invalid_source_asset", "source_asset_id must identify a video asset")

    if legacy_mode not in {"auto", "text", "fl2va", "ref2va"}:
        raise ApiError(400, "invalid_mode", "mode must be auto, text, fl2va, or ref2va")

    source_reference = next((item for item in references if item.asset_id == source_id), None)
    if source_id:
        if source_reference is None:
            raise ApiError(400, "source_not_connected", "source_asset_id must be an already-connected video reference")
        if source_reference.kind != "video":
            raise ApiError(400, "invalid_source_asset", "source_asset_id must match the connected video")
        if source_reference.include_audio:
            raise ApiError(
                400, "source_audio_unsupported",
                "Director source audio and mute modes are not implemented; source video must use generated audio",
            )
        # Stable partition: source first, all other user ordering unchanged.
        references = [source_reference, *(item for item in references if item.asset_id != source_id)]

    if requested == "auto":
        if source_id:
            resolved = "v2v" if len(references) == 1 else "rv2v"
        elif legacy_mode == "text":
            resolved = "t2v"
        elif legacy_mode == "ref2va":
            resolved = "r2v"
        elif legacy_mode == "fl2va":
            resolved = (
                "i2v"
                if len(references) == 1 and references[0].kind == "image"
                and references[0].role == "first_frame"
                else "fl2v"
            )
        elif not references:
            resolved = "t2v"
        elif (
            len(references) <= 2
            and all(item.kind == "image" for item in references)
            and all(item.role in {"first_frame", "last_frame"} for item in references)
        ):
            resolved = (
                "i2v"
                if len(references) == 1 and references[0].role == "first_frame"
                else "fl2v"
            )
        else:
            resolved = "r2v"
    else:
        resolved = requested

    expected_legacy = (
        "text" if resolved == "t2v"
        else "fl2va" if resolved in {"i2v", "fl2v"}
        else "ref2va"
    )
    if legacy_mode != "auto" and legacy_mode != expected_legacy:
        raise ApiError(400, "director_mode_conflict", "director_mode conflicts with the legacy mode parameter")

    if resolved == "t2v":
        if references or source_id:
            raise ApiError(400, "director_contract", "t2v does not accept references or source_asset_id")
    elif resolved == "i2v":
        if source_id or len(references) != 1 or references[0].kind != "image" or references[0].role != "first_frame":
            raise ApiError(400, "director_contract", "i2v requires exactly one first_frame image and no source_asset_id")
    elif resolved == "fl2v":
        roles = [item.role for item in references]
        if (
            source_id or not 1 <= len(references) <= 2
            or any(item.kind != "image" for item in references)
            or any(role not in {"first_frame", "last_frame"} for role in roles)
            or len(set(roles)) != len(roles)
        ):
            raise ApiError(400, "director_contract", "fl2v requires one or two unique endpoint images and no source_asset_id")
    elif resolved == "r2v":
        if source_id or not references or all(item.kind == "audio" for item in references):
            raise ApiError(
                400, "director_contract",
                "r2v requires image or video references; H3 does not support an audio-only reference set or source_asset_id",
            )
    elif resolved == "v2v":
        if not source_id or len(references) != 1 or references[0].asset_id != source_id or references[0].kind != "video":
            raise ApiError(400, "director_contract", "v2v requires exactly one connected source video")
    else:  # rv2v
        other = [item for item in references if item.asset_id != source_id]
        videos = [item for item in references if item.kind == "video"]
        if (
            not source_id or not source_reference or len(videos) != 1
            or videos[0].asset_id != source_id
            or any(item.kind not in {"image", "audio"} for item in other)
        ):
            raise ApiError(
                400, "director_contract",
                "rv2v requires one connected source video plus optional image/audio references; a second video is not allowed",
            )
    return requested, resolved, references


def _bind_director_source_prompt(prompt: str, director_mode: str, source_asset_id: str) -> str:
    """Make the source binding explicit in the compiled prompt only.

    Stored authored text remains untouched.  This adds no creative prose: it
    only supplies the native H3 source token when the author did not mention
    the connected source asset themselves.
    """

    if (
        director_mode in {"v2v", "rv2v"} and source_asset_id
        and re.search(r"<Video\s+1>", prompt, re.IGNORECASE) is None
    ):
        return f"{prompt}\n\n<Video 1>"
    return prompt


def _validate_reference_counts(references: list[AssetRef], output_type: str) -> None:
    if output_type == "image":
        if len(references) > 10:
            raise ApiError(400, "too_many_references", "image generation supports at most 10 reference images")
        return
    if len(references) > H3_MAX_REFERENCES:
        raise ApiError(400, "too_many_references", f"H3 supports at most {H3_MAX_REFERENCES} total reference files")
    for kind, maximum in H3_REFERENCE_CAPACITY.items():
        count = sum(ref.kind == kind for ref in references)
        if kind == "audio":
            count += sum(ref.kind == "video" and ref.include_audio for ref in references)
        if count > maximum:
            raise ApiError(400, "too_many_references", f"H3 supports at most {maximum} {kind} references")


def _validate_reference_roles(output_type: str, references: list[AssetRef]) -> None:
    for reference in references:
        allowed = IMAGE_ROLES if output_type == "image" else VIDEO_ROLES.get(reference.kind, set())
        if reference.role not in allowed:
            raise ApiError(
                400,
                "invalid_role",
                f"role {reference.role!r} is not valid for a {reference.kind} reference in {output_type} generation",
            )
        if output_type == "video" and reference.kind == "audio" and reference.role == "voice":
            if re.fullmatch(r"S[1-9]\d*", reference.voice_speaker) is None:
                raise ApiError(400, "invalid_voice_binding", "voice references require an explicit target speaker such as S1")
            if reference.voice_subject < 1:
                raise ApiError(400, "invalid_voice_binding", "voice references require an explicit target Subject number")


def _validate_reference_media(references: list[AssetRef]) -> tuple[float, float]:
    """Enforce H3 Ref2VA's per-clip and per-modality 15-second budgets."""
    video_total = 0.0
    audio_total = 0.0
    for reference in references:
        if reference.kind not in {"video", "audio"}:
            continue
        if reference.duration <= 0:
            raise ApiError(400, "missing_media_metadata", "video and audio references must be uploaded again so duration can be verified")
        if not 2 <= reference.duration <= 15:
            raise ApiError(400, "invalid_reference_duration", f"each {reference.kind} reference must be between 2 and 15 seconds")
        if reference.kind == "video":
            video_total += reference.duration
            if reference.fps <= 0 or not math.isclose(reference.fps, 24.0, abs_tol=0.01):
                raise ApiError(400, "invalid_reference_fps", "reference videos must be normalized to 24 fps during upload")
            if reference.include_audio:
                if not reference.has_audio:
                    raise ApiError(400, "missing_audio_track", "the selected reference video has no audio track")
                audio_total += reference.duration
        else:
            audio_total += reference.duration
    if video_total > 15.0001:
        raise ApiError(400, "reference_duration_budget", "reference videos may total at most 15 seconds")
    if audio_total > 15.0001:
        raise ApiError(400, "reference_duration_budget", "selected reference audio may total at most 15 seconds")
    return video_total, audio_total


def _tagged_prompt(
    prompt: str,
    references: tuple[AssetRef, ...],
    *,
    append_missing: bool = True,
) -> str:
    counts = {"image": 0, "video": 0, "audio": 0}
    names = {"image": "Picture", "video": "Video", "audio": "Audio"}
    tags: list[str] = []
    alias_tags: dict[str, set[str]] = {}
    for reference in references:
        counts[reference.kind] += 1
        tag = f"<{names[reference.kind]} {counts[reference.kind]}>"
        tags.append(tag)
        for alias in reference.aliases:
            alias = alias.strip()
            if alias:
                alias_tags.setdefault(alias, set()).add(tag)

    def resolve(alias: str) -> str:
        matches = alias_tags.get(alias)
        if not matches:
            raise ApiError(400, "unknown_reference", f"@{{{alias}}} is not connected to this generator")
        if len(matches) != 1:
            raise ApiError(400, "ambiguous_reference", f"reference alias {alias!r} is ambiguous; use the asset id")
        return next(iter(matches))

    result = REFERENCE_TOKEN_PATTERN.sub(lambda match: resolve(match.group(1)), prompt)
    # Bare mentions remain convenient for filenames. Match all aliases in one
    # longest-first pass so `cat.png.copy` cannot be consumed as `cat.png`.
    aliases = sorted(alias_tags, key=len, reverse=True)
    if aliases:
        alternation = "|".join(re.escape(alias) for alias in aliases)
        bare = re.compile(rf"(?<![\w@])@({alternation})(?![\w.\-/])")
        result = bare.sub(lambda match: resolve(match.group(1)), result)
    dangling = re.search(r"(?<![\w@])@([^\s@<>]+)", result)
    if dangling:
        raise ApiError(400, "unknown_reference", f"@{dangling.group(1)} is not connected to this generator")

    available = {"picture": counts["image"], "video": counts["video"], "audio": counts["audio"]}
    for kind, number in TAG_PATTERN.findall(result):
        if int(number) < 1 or int(number) > available[kind.lower()]:
            raise ApiError(
                400,
                "invalid_reference_tag",
                f"<{kind} {number}> has no connected reference",
            )
    missing = [tag for tag in tags if tag.lower() not in result.lower()]
    if append_missing and missing:
        result = f"{result}\n\nUse {', '.join(missing)} as visual or audio references."
    return result


def _prompt_mode(data: dict[str, Any]) -> str:
    value = data.get("prompt_mode", "default")
    if not isinstance(value, str) or value not in PROMPT_MODES:
        raise ApiError(
            400,
            "invalid_parameter",
            "prompt_mode must be default or preserve_tags_only",
        )
    return value


def _preserve_tags_only_prompt(
    prompt: str,
    references: tuple[AssetRef, ...],
    parts: Any,
) -> str:
    """Preserve authored text and only resolve stable reference tokens.

    The editable prompt is the only authored source in this mode. Historical
    structured fields are deliberately ignored so stale UI state can never be
    appended to the model prompt.
    """

    del parts
    return replace_reference_tokens(prompt, references)


def _flux2_reference_text(text: str, references: tuple[AssetRef, ...]) -> str:
    """Resolve canvas mentions to FLUX.2's documented image-1/image-2 syntax."""

    aliases: dict[str, set[str]] = {}
    for index, reference in enumerate(references, start=1):
        tag = f"image {index}"
        for alias in reference.aliases:
            alias = alias.strip()
            if alias:
                aliases.setdefault(alias, set()).add(tag)

    def resolve(alias: str) -> str:
        matches = aliases.get(alias)
        if not matches:
            raise ApiError(400, "unknown_reference", f"@{{{alias}}} is not connected to this generator")
        if len(matches) != 1:
            raise ApiError(400, "ambiguous_reference", f"reference alias {alias!r} is ambiguous; use the asset id")
        return next(iter(matches))

    result = REFERENCE_TOKEN_PATTERN.sub(lambda match: resolve(match.group(1)), text)
    names = sorted(aliases, key=len, reverse=True)
    if names:
        alternation = "|".join(re.escape(alias) for alias in names)
        bare = re.compile(rf"(?<![\w@])@({alternation})(?![\w.\-/])")
        result = bare.sub(lambda match: resolve(match.group(1)), result)
    dangling = re.search(r"(?<![\w@])@([^\s@<>]+)", result)
    if dangling:
        raise ApiError(400, "unknown_reference", f"@{dangling.group(1)} is not connected to this generator")

    # The canvas is localized, while FLUX.2's documented prompt syntax is
    # consistently ``image N``.  Accept the common localized/English forms,
    # validate them against the connected ordered references, and send one
    # canonical spelling to the model.
    reference_pattern = re.compile(
        r"(?:<\s*)?(?:(?<![A-Za-z0-9_])(?:image|picture)\s*#?\s*(\d+)(?!\s*[Kk:]|[A-Za-z0-9_])|(?:图片|图)\s*#?\s*(\d+)(?!\s*[Kk:]|\d))(?:\s*>)?",
        re.IGNORECASE,
    )

    def canonical_reference(match: re.Match[str]) -> str:
        number = int(match.group(1) or match.group(2))
        if number < 1 or number > len(references):
            raise ApiError(
                400,
                "invalid_reference_tag",
                f"image {number} has no connected reference",
            )
        return f"image {number}"

    result = reference_pattern.sub(canonical_reference, result)
    return result


def _compile_flux2_prompt(
    prompt: str,
    references: tuple[AssetRef, ...],
    parts: Any,
) -> str:
    transformed_parts = parts
    if isinstance(parts, dict):
        transformed_parts = {
            key: _flux2_reference_text(value, references) if isinstance(value, str) else value
            for key, value in parts.items()
        }
    return compile_prompt(
        _flux2_reference_text(prompt, references),
        mode="text-to-image",
        references=(),
        parts=transformed_parts,
    )


def _compile_qwen21_prompt(
    prompt: str,
    references: tuple[AssetRef, ...],
    parts: Any,
) -> str:
    """Bind ordered canvas references to Qwen-Image 2.1's native <imageN> tags."""

    normalized = _compile_flux2_prompt(prompt, references, parts)
    return re.sub(r"(?<![A-Za-z0-9_])image ([1-9]\d*)(?![A-Za-z0-9_])", r"<image\1>", normalized)


def _legacy_compile_prompt(
    prompt: str,
    *,
    mode: str,
    references: tuple[AssetRef, ...] = (),
    parts: Any = None,
    duration_actual: float = 0.0,
) -> str:
    """Compile UI prompt parts into H3's concise, temporally explicit form."""
    values: dict[str, str] = {}
    if isinstance(parts, dict):
        for key in ("subject", "action", "scene", "camera", "light", "style", "dialogue", "sound", "music"):
            value = parts.get(key, "")
            if isinstance(value, str) and value.strip():
                values[key] = value.strip()
    base = prompt.strip()
    segments = [
        values.get("subject", ""), values.get("action", ""), values.get("scene", ""),
        values.get("camera", ""), values.get("light", ""), values.get("style", ""),
    ]
    structured = "; ".join(segment for segment in segments if segment)
    base = "; ".join(filter(None, (base, structured)))
    if mode in {"text-to-image", "image-to-image"}:
        return _tagged_prompt(base, references).strip()

    sound_parts = []
    if values.get("dialogue"):
        sound_parts.append(f"Dialogue/voiceover (preserve the original language and wording): {values['dialogue']}")
    if values.get("sound"):
        sound_parts.append(f"Diegetic ambience and action sounds: {values['sound']}")
    sound = " ".join(sound_parts) or "Natural diegetic sound matching the scene."
    music = values.get("music", "N/A")
    if mode == "ref2va":
        counters = {"image": 0, "video": 0, "audio": 0}
        family = {"image": "Picture", "video": "Video", "audio": "Audio"}
        definitions: list[str] = []
        retention: list[str] = []
        directives: list[str] = []
        subject_index = 0
        for reference in references:
            counters[reference.kind] += 1
            tag = f"<{family[reference.kind]} {counters[reference.kind]}>"
            if reference.kind == "image" and reference.role == "identity":
                subject_index += 1
                definitions.append(f"<Subject {subject_index}> is the person or object shown in {tag}.")
                retention.append(f"<Subject {subject_index}> ({tag}): fully_preserved - preserve identity and defining appearance.")
            elif reference.kind == "video" and reference.role == "motion":
                subject_index += 1
                definitions.append(f"<Subject {subject_index}> is the transferable motion pattern demonstrated by {tag}, not the source identity.")
                retention.append(f"<Subject {subject_index}> ({tag}): attribute_transfer - transfer motion only and preserve the target identity.")
            elif reference.kind == "image":
                strength = "attribute_transfer" if reference.role in {"style", "composition"} else "weak_reference"
                directives.append(f"{tag} provides {reference.role}; use {strength} and do not copy unrelated identity attributes.")
            elif reference.kind == "video":
                directives.append(f"{tag} provides {reference.role}; transfer that temporal/camera attribute without copying source identity.")
            else:
                audio_strength = "fully_copy" if reference.role == "voice" else "partially_copy" if reference.role in {"music", "rhythm"} else "reference"
                directives.append(f"{tag} provides {reference.role}; audio retention is {audio_strength}.")
            if reference.kind == "video" and reference.include_audio:
                directives.append(f"Also use the synchronized audio track paired with {tag}; keep it temporally aligned with that video reference.")
        definition_text = "\n".join(definitions) or "N/A"
        retention_text = "\n".join(retention) or "N/A"
        compiled = (
            f"subject_definitions:\n{definition_text}\n\n"
            f"summary: [reference generation] {base}\nReference directives: {' '.join(directives) or 'N/A'}\n\n"
            f"retention_analysis:\n{retention_text}\n\n"
            f"detailed_description: [Shot 1] {base}\nReference directives: {' '.join(directives) or 'N/A'}\n\n"
            f"overall_soundscape: {sound}\n\n"
            f"non_diegetic_music: {music}"
        )
    else:
        alignment = ""
        if references:
            first_index = next((index for index, ref in enumerate(references, 1) if ref.role == "first_frame"), None)
            last_index = next((index for index, ref in enumerate(references, 1) if ref.role == "last_frame"), None)
            shot_numbers = [int(number) for number in re.findall(r"\[Shot\s+(\d+)\]", base, flags=re.IGNORECASE)]
            final_shot = max(shot_numbers, default=1)
            if first_index and last_index:
                alignment = (
                    "How the reference pictures align with the target video — "
                    f"<Picture {first_index}> (from Shot 1) aligns with the 0.00-second mark; "
                    f"<Picture {last_index}> (from Shot {final_shot}) aligns with the {duration_actual:.2f}-second mark.\n\n"
                )
            elif first_index:
                alignment = f"For the target video, at 0.00 seconds, <Picture {first_index}> (from [Shot 1]) is fully referenced.\n\n"
            elif last_index:
                alignment = f"For the target video, at {duration_actual:.2f} seconds, <Picture {last_index}> (from [Shot {final_shot}]) is fully referenced.\n\n"
        compiled = (
            f"{alignment}integrated_multimodal_description: [Shot 1] {base}\n\n"
            f"overall_soundscape: {sound}\n\n"
            f"non_diegetic_music: {music}"
        )
    return _tagged_prompt(compiled, references).strip()


def compile_prompt_request(data: Any, lookup_asset: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """Public prompt-preview contract used by POST /api/prompts/compile."""
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "request body must be an object")
    output_type = str(data.get("output_type", "video")).lower()
    if output_type not in {"video", "image"}:
        raise ApiError(400, "invalid_output_type", "output_type must be video or image")
    references, graph_prompt = _graph_references(data.get("graph"), lookup_asset, output_type)
    if not references:
        references = _explicit_references(data.get("references", data.get("assets")), lookup_asset)
    _validate_reference_counts(references, output_type)
    _validate_reference_roles(output_type, references)
    parameters = data.get("parameters", {}) if isinstance(data.get("parameters", {}), dict) else {}
    requested = str(data.get("mode", parameters.get("mode", "auto"))).lower()
    requested_profile = str(data.get("profile_id", "auto"))
    selected_profile = DEFAULT_REGISTRY.get(requested_profile) if requested_profile != "auto" else None
    director_mode = ""
    requested_director_mode = ""
    source_asset_id = ""
    if output_type == "image":
        mode = "text-to-image" if not references else "image-to-image"
    else:
        if selected_profile and requested == "auto":
            if selected_profile.compiler == "h3_ref":
                requested = "ref2va"
            elif selected_profile.compiler == "h3_fl":
                requested = "fl2va" if references else "text"
        requested_director_mode, director_mode, references = _resolve_director_contract(
            data, parameters, references, lookup_asset, requested,
        )
        source_asset_id = str(_director_value(data, parameters, "source_asset_id", "") or "")
        compiler_mode = (
            "text" if director_mode == "t2v"
            else "fl2va" if director_mode in {"i2v", "fl2v"}
            else "ref2va"
        )
        mode = _select_mode(compiler_mode, references)
    if mode == "ref2va":
        _validate_reference_media(references)
    prompt_mode = _prompt_mode(data) if output_type == "video" else "default"
    prompt = _string(
        data.get("prompt", graph_prompt), "prompt", 12_000,
        required=not bool(data.get("parts", data.get("prompt_parts"))),
        preserve_whitespace=prompt_mode == "preserve_tags_only",
    )
    duration = float(_number(parameters.get("duration", data.get("duration", 5)), "duration", minimum=5, maximum=H3_MAX_DURATION_SECONDS)) if output_type == "video" else 0.0
    duration_actual = h3_frame_count(duration) / 24 if output_type == "video" else 0.0
    if selected_profile and selected_profile.compiler == "qwen_image_21":
        if output_type != "image" or len(references) > 10 or any(reference.kind != "image" for reference in references):
            raise ApiError(400, "profile_mismatch", "Qwen-Image 2.1 accepts text and up to ten image references")
        if "denoise" in parameters:
            raise ApiError(400, "invalid_parameter", "Qwen-Image 2.1 does not expose latent denoise")
        compiled = _compile_qwen21_prompt(
            prompt, tuple(references), data.get("parts", data.get("prompt_parts")),
        )
    elif selected_profile and selected_profile.compiler == "flux2_klein":
        if output_type != "image" or len(references) > 4 or any(reference.kind != "image" for reference in references):
            raise ApiError(400, "profile_mismatch", "FLUX.2 Klein accepts zero to four image references")
        preview_negative = _string(data.get("negative_prompt", parameters.get("negative_prompt", "")), "negative_prompt", 6_000)
        if preview_negative.strip():
            raise ApiError(400, "invalid_parameter", "FLUX.2 Klein does not use a negative prompt")
        compiled = _compile_flux2_prompt(
            prompt, tuple(references), data.get("parts", data.get("prompt_parts")),
        )
    elif output_type == "video" and prompt_mode == "preserve_tags_only":
        compiled = _preserve_tags_only_prompt(
            prompt, tuple(references), data.get("parts", data.get("prompt_parts")),
        )
    else:
        compiled = compile_prompt(prompt, mode=mode, references=tuple(references), parts=data.get("parts", data.get("prompt_parts")), duration_actual=duration_actual)
    if output_type == "video":
        compiled = _bind_director_source_prompt(compiled, director_mode, source_asset_id)
    if not compiled:
        raise ApiError(400, "invalid_parameter", "prompt or prompt parts are required")
    result = {
        "prompt": compiled, "mode": mode, "reference_count": len(references),
        "duration_actual": round(duration_actual, 3),
    }
    if output_type == "video":
        result.update({
            "director_mode": director_mode,
            "resolved_director_mode": director_mode,
            "requested_director_mode": requested_director_mode,
            "source_asset_id": source_asset_id or None,
        })
    return result


def parse_generation_request(
    data: Any,
    lookup_asset: Callable[[str], dict[str, Any]],
    registry: ProfileRegistry = DEFAULT_REGISTRY,
) -> GenerationSpec:
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "request body must be a JSON object")
    output_type = str(
        data.get("output_type", data.get("type", data.get("kind", "video")))
    ).lower()
    if output_type not in {"video", "image"}:
        raise ApiError(400, "invalid_output_type", "output_type must be video or image")
    parameters = data.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ApiError(400, "invalid_parameter", "parameters must be an object")
    parameters = dict(parameters)
    aliases = {
        "aspectRatio": "aspect_ratio",
        "duration": "duration",
        "steps": "steps",
        "loraStrength": "lora_strength",
        "seed": "seed",
        "modelMode": "mode",
        "refImageSize": "ref_image_size",
        "width": "width",
        "height": "height",
        "cfg": "cfg",
        "denoise": "denoise",
        "negativePrompt": "negative_prompt",
    }
    for source, destination in aliases.items():
        if source in data and destination not in parameters:
            parameters[destination] = data[source]
    allowed_parameters = {
        "aspect_ratio", "resolution", "width", "height", "steps", "seed",
        "denoise", "lora_strength",
    }
    if output_type == "image":
        allowed_parameters.update({"cfg", "negative_prompt"})
    else:
        allowed_parameters.update({
            "duration", "ref_image_size", "mode", "director_mode", "source_asset_id",
        })
    unknown_parameters = sorted(set(parameters) - allowed_parameters)
    if unknown_parameters:
        raise ApiError(
            400, "invalid_parameter",
            f"unknown generation parameter: {unknown_parameters[0]}",
        )
    references, graph_prompt = _graph_references(data.get("graph"), lookup_asset, output_type)
    explicit = _explicit_references(
        data.get("references", data.get("assets")), lookup_asset
    )
    if explicit and not references:
        references = explicit
    _validate_reference_counts(references, output_type)
    _validate_reference_roles(output_type, references)
    prompt_mode = _prompt_mode(data) if output_type == "video" else "default"
    raw_prompt = _string(
        data.get("prompt", graph_prompt), "prompt", 12_000,
        required=not bool(data.get("parts", data.get("prompt_parts"))),
        preserve_whitespace=prompt_mode == "preserve_tags_only",
    )
    negative = _string(data.get("negative_prompt", parameters.get("negative_prompt", "")), "negative_prompt", 6_000)
    seed = _seed(parameters.get("seed", -1))

    if output_type == "image":
        if any(reference.kind != "image" for reference in references):
            raise ApiError(400, "invalid_references", "image generation only accepts image references")
        mode = "image-to-image" if references else "text-to-image"
        requested_profile = str(data.get("profile_id", "auto"))
        profile = registry.choose(output_type, mode, references, requested_profile)
        _validate_profile_identity(data, requested_profile, profile)
        width, height = _resolution(
            parameters, image=True, qwen_image_21=profile.compiler == "qwen_image_21",
        )
        expected = {
            "checkpoint_img2img", "z_image_img2img", "z_image_lora_img2img",
            "qwen_image_edit", "qwen_image_21", "flux2_klein",
        } if references else {
            "checkpoint_t2i", "z_image_t2i", "z_image_lora_t2i", "qwen_image_t2i", "qwen_image_21", "flux2_klein",
        }
        if profile.compiler not in expected:
            raise ApiError(400, "profile_mismatch", f"profile does not support {mode}")
        if profile.compiler == "flux2_klein":
            if "denoise" in parameters:
                raise ApiError(400, "invalid_parameter", "FLUX.2 Klein uses reference conditioning and does not expose denoise")
            if negative.strip():
                raise ApiError(400, "invalid_parameter", "FLUX.2 Klein does not use a negative prompt")
            if width % 16 or height % 16:
                raise ApiError(400, "invalid_resolution", "FLUX.2 width and height must be multiples of 16")
            prompt = _compile_flux2_prompt(
                raw_prompt, tuple(references), data.get("parts", data.get("prompt_parts")),
            )
        elif profile.compiler == "qwen_image_21":
            if "denoise" in parameters:
                raise ApiError(400, "invalid_parameter", "Qwen-Image 2.1 uses instruction-conditioned editing and does not expose latent denoise")
            prompt = _compile_qwen21_prompt(
                raw_prompt, tuple(references), data.get("parts", data.get("prompt_parts")),
            )
        else:
            prompt = compile_prompt(
                raw_prompt,
                mode="text-to-image" if profile.compiler == "qwen_image_edit" else mode,
                references=() if profile.compiler == "qwen_image_edit" else tuple(references),
                parts=data.get("parts", data.get("prompt_parts")),
            )
        if profile.compiler == "qwen_image_t2i":
            uses_chinese = any("\u3400" <= char <= "\u9fff" for char in prompt)
            suffix = "，超清，4K，电影级构图。" if uses_chinese else ", Ultra HD, 4K, cinematic composition."
            if "4k" not in prompt.lower():
                prompt = prompt.rstrip("。. ") + suffix
        sampler = "res_multistep" if profile.compiler in {
            "z_image_t2i", "z_image_img2img", "z_image_lora_t2i", "z_image_lora_img2img",
        } else (
            "euler" if profile.compiler in {"qwen_image_t2i", "qwen_image_edit", "qwen_image_21", "flux2_klein"} else "euler_ancestral"
        )
        scheduler = "flux2" if profile.compiler == "flux2_klein" else (
            "simple" if profile.compiler in {
                "z_image_t2i", "z_image_img2img", "z_image_lora_t2i", "z_image_lora_img2img",
                "qwen_image_t2i", "qwen_image_edit", "qwen_image_21",
            } else "normal"
        )
        return GenerationSpec(
            output_type="image",
            prompt=prompt,
            negative_prompt=negative,
            width=width,
            height=height,
            steps=int(_profile_number(profile, parameters, "steps", 24, minimum=1, maximum=100, integer=True)),
            seed=seed,
            references=tuple(references),
            mode=mode,
            cfg=float(_profile_number(profile, parameters, "cfg", 7, minimum=1, maximum=30)),
            denoise=float(_profile_number(profile, parameters, "denoise", 0.65 if references else 1, minimum=0.05, maximum=1)),
            lora_strength=float(_profile_number(
                profile, parameters, "lora_strength", 0, minimum=0, maximum=2,
            )),
            profile_id=profile.id,
            profile_version=profile.version,
            profile_digest=profile.digest(),
            compiler=profile.compiler,
            sampling_mode=profile.sampling_mode,
            sampler=sampler,
            scheduler=scheduler,
            image_lora=profile.model_bindings.get("image_lora", ""),
            model_bindings=dict(profile.model_bindings),
        )

    requested_profile = str(data.get("profile_id", "auto"))
    requested_mode = str(parameters.get("mode", data.get("mode", "auto"))).lower()
    if requested_profile != "auto" and requested_mode == "auto":
        selected_compiler = registry.get(requested_profile).compiler
        if selected_compiler == "h3_ref":
            requested_mode = "ref2va"
        elif selected_compiler == "h3_fl":
            requested_mode = "fl2va" if references else "text"
    requested_director_mode, director_mode, references = _resolve_director_contract(
        data, parameters, references, lookup_asset, requested_mode,
    )
    source_asset_id = str(_director_value(data, parameters, "source_asset_id", "") or "")
    compiler_mode = (
        "text" if director_mode == "t2v"
        else "fl2va" if director_mode in {"i2v", "fl2v"}
        else "ref2va"
    )
    mode = _select_mode(compiler_mode, references)
    if mode == "fl2va":
        if len({ref.role for ref in references}) != len(references):
            raise ApiError(400, "invalid_references", "first/last frame roles must be unique")
    if references and all(reference.kind == "audio" for reference in references):
        raise ApiError(400, "audio_only_unsupported", "H3 does not support an audio-only reference set")
    profile = registry.choose(output_type, mode, references, requested_profile)
    _validate_profile_identity(data, requested_profile, profile)
    width, height = _resolution(parameters, image=False)
    expected_compiler = "h3_ref" if mode == "ref2va" else "h3_fl"
    if profile.compiler != expected_compiler:
        raise ApiError(400, "profile_mismatch", f"profile does not support resolved mode {mode}")
    duration = float(_profile_number(profile, parameters, "duration", 5, minimum=5, maximum=H3_MAX_DURATION_SECONDS))
    if mode == "ref2va":
        _validate_reference_media(references)
    frames = h3_frame_count(duration)
    prompt = (
        _preserve_tags_only_prompt(
            raw_prompt, tuple(references), data.get("parts", data.get("prompt_parts")),
        )
        if prompt_mode == "preserve_tags_only"
        else compile_prompt(raw_prompt, mode=mode, references=tuple(references), parts=data.get("parts", data.get("prompt_parts")), duration_actual=frames / 24)
    )
    prompt = _bind_director_source_prompt(prompt, director_mode, source_asset_id)
    ref_image_size = str(parameters.get("ref_image_size", "match")).lower()
    if ref_image_size not in {"match", "max"}:
        raise ApiError(400, "invalid_parameter", "ref_image_size must be match or max")
    turbo = profile.sampling_mode == "turbo4"
    if not turbo and "lora_strength" in parameters and _number(parameters["lora_strength"], "lora_strength", minimum=0, maximum=2) != 0:
        raise ApiError(400, "invalid_parameter", "base sampling does not load a Turbo LoRA; lora_strength must be 0")
    steps = int(_profile_number(profile, parameters, "steps", 4 if turbo else 20, minimum=4, maximum=50, integer=True))
    return GenerationSpec(
        output_type="video",
        prompt=prompt,
        negative_prompt="",
        width=width,
        height=height,
        steps=steps,
        seed=seed,
        references=tuple(references),
        mode=mode,
        prompt_mode=prompt_mode,
        duration=duration,
        frames=frames,
        lora_strength=float(_profile_number(profile, parameters, "lora_strength", 0.75, minimum=0, maximum=2)) if turbo else 0,
        denoise=float(_profile_number(profile, parameters, "denoise", 1.0, minimum=0.05, maximum=1)),
        ref_image_size=ref_image_size,
        profile_id=profile.id,
        profile_version=profile.version,
        profile_digest=profile.digest(),
        compiler=profile.compiler,
        sampling_mode=profile.sampling_mode,
        sampler="sa_solver" if turbo else "res_multistep",
        scheduler="simple",
        model_bindings=dict(profile.model_bindings),
        reference_duration_total=sum(reference.duration for reference in references),
        director_mode=director_mode,
        requested_director_mode=requested_director_mode,
        source_asset_id=source_asset_id,
    )


def compile_video_workflow(
    spec: GenerationSpec, config: Config, job_id: str,
    resume_plan: ResumeSamplingPlan | None = None,
    motion_context_plan: MotionContextPlan | None = None,
) -> dict[str, Any]:
    if spec.output_type != "video":
        raise ValueError("video spec required")
    if resume_plan is not None and motion_context_plan is not None:
        raise ValueError("resumable sampling and Motion Context cannot share one render")
    if spec.director_mode in {"v2v", "rv2v"}:
        source = next((item for item in spec.references if item.asset_id == spec.source_asset_id), None)
        videos = [item for item in spec.references if item.kind == "video"]
        if (
            spec.mode != "ref2va" or source is None or source.kind != "video"
            or not videos or videos[0].asset_id != spec.source_asset_id
            or (spec.director_mode == "v2v" and len(spec.references) != 1)
            or (spec.director_mode == "rv2v" and len(videos) != 1)
        ):
            raise CapabilityError("Director source workflow contract is inconsistent")
    reference_mode = spec.mode == "ref2va"
    model = lambda role: spec.model_bindings.get(role, str(getattr(config, role)))
    workflow: dict[str, Any] = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": model("ref_model") if reference_mode else model("fl_model"),
                "weight_dtype": "default",
            },
        },
        "2": {
            "class_type": "PathchSageAttentionKJ",
            "inputs": {"model": ["1", 0], "sage_attention": "auto", "allow_compile": False},
        },
        "3": {
            "class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch",
            "inputs": {"model": ["2", 0]},
        },
        "5": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": model("text_encoder"), "type": "minimax", "device": "default"},
        },
        "6": {"class_type": "VAELoader", "inputs": {"vae_name": model("video_vae")}},
        "7": {"class_type": "VAELoader", "inputs": {"vae_name": model("audio_vae")}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": spec.seed}},
        "10": {
            "class_type": "BasicGuider",
            "inputs": {"model": ["4" if spec.sampling_mode == "turbo4" else "3", 0], "conditioning": ["8", 0]},
        },
        "11": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": spec.sampler or "sa_solver"}},
        "12": {
            "class_type": "BasicScheduler",
            "inputs": {"model": ["4" if spec.sampling_mode == "turbo4" else "3", 0], "scheduler": spec.scheduler or "simple", "steps": resume_plan.max_total_steps if resume_plan else spec.steps, "denoise": spec.denoise},
        },
        "13": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["9", 0],
                "guider": ["10", 0],
                "sampler": ["11", 0],
                "sigmas": ["12", 0],
                "latent_image": ["8", 1],
            },
        },
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["6", 0]}},
        "15": {
            "class_type": "VAEDecodeAudio",
            "inputs": {"samples": ["13", 0], "vae": ["7", 0]},
        },
        "16": {
            "class_type": "CreateVideo",
            "inputs": {"images": ["14", 0], "audio": ["15", 0], "fps": 24.0, "bit_depth": 8},
        },
        "17": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["16", 0],
                "filename_prefix": f"h3-studio/videos/{job_id}",
                "format": "mp4",
                "codec": "auto",
            },
        },
    }
    if spec.sampling_mode == "turbo4":
        workflow["4"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["3", 0],
                "lora_name": model("ref_lora") if reference_mode else model("fl_lora"),
                "strength_model": spec.lora_strength,
            },
        }

    if resume_plan is not None:
        # Output 0 is the current sampler state required for true continuation;
        # output 1 is the denoised estimate suitable for the user-facing preview.
        workflow["14"]["inputs"]["samples"] = ["13", 1]
        workflow["15"]["inputs"]["samples"] = ["13", 1]
        workflow["18"] = {
            "class_type": "SplitSigmas",
            "inputs": {"sigmas": ["12", 0], "step": spec.steps if resume_plan.mode == "initial" else resume_plan.steps_before},
        }
        if resume_plan.mode == "initial":
            workflow["13"]["inputs"]["sigmas"] = ["18", 0]
        else:
            workflow["9"] = {"class_type": "DisableNoise", "inputs": {}}
            workflow["20"] = {
                "class_type": "SplitSigmas",
                "inputs": {"sigmas": ["18", 1], "step": resume_plan.additional_steps},
            }
            workflow["21"] = {
                "class_type": "H3StudioLoadLatent",
                "inputs": {"latent": resume_plan.checkpoint_input},
            }
            workflow["13"]["inputs"].update({
                "noise": ["9", 0], "sigmas": ["20", 0], "latent_image": ["21", 0],
            })
        workflow["19"] = {
            "class_type": "H3StudioSaveLatent",
            "inputs": {
                "samples": ["13", 0],
                # SaveVideo is the primary output. Checkpoint I/O may only run
                # after that file exists, and the custom node is best effort.
                "video_done": ["17", 0],
                "filename_prefix": f"h3-studio/checkpoints/{job_id}",
            },
        }

    conditioning: dict[str, Any]
    if reference_mode:
        conditioning = {
            "clip": ["5", 0],
            "vae": ["6", 0],
            "audio_vae": ["7", 0],
            "prompt": spec.prompt,
            "width": spec.width,
            "height": spec.height,
            "length": spec.frames,
            "ref_image_size": spec.ref_image_size,
        }
        counters = {"image": 0, "video": 0, "audio": 0}
        for offset, reference in enumerate(spec.references):
            node_id = str(100 + offset * 2)
            index = counters[reference.kind]
            counters[reference.kind] += 1
            if reference.kind == "image":
                workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": reference.comfy_path}}
                conditioning[f"ref_images.ref_image_{index}"] = [node_id, 0]
            elif reference.kind == "video":
                split_id = str(int(node_id) + 1)
                workflow[node_id] = {"class_type": "LoadVideo", "inputs": {"file": reference.comfy_path}}
                workflow[split_id] = {
                    "class_type": "GetVideoComponents",
                    "inputs": {"video": [node_id, 0]},
                }
                conditioning[f"ref_videos.ref_video_{index}"] = [split_id, 0]
                if reference.include_audio:
                    conditioning[f"ref_video_audios.ref_video_audio_{index}"] = [split_id, 1]
            else:
                workflow[node_id] = {"class_type": "LoadAudio", "inputs": {"audio": reference.comfy_path}}
                conditioning[f"ref_audios.ref_audio_{index}"] = [node_id, 0]
        workflow["8"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": conditioning}
    else:
        conditioning = {
            "clip": ["5", 0],
            "vae": ["6", 0],
            "prompt": spec.prompt,
            "width": spec.width,
            "height": spec.height,
            "length": spec.frames,
        }
        for offset, reference in enumerate(spec.references):
            node_id = str(100 + offset)
            workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": reference.comfy_path}}
            conditioning[reference.role] = [node_id, 0]
        workflow["8"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": conditioning}

    if motion_context_plan is not None:
        if motion_context_plan.checkpoint_input:
            workflow["21"] = {
                "class_type": "H3StudioLoadLatent",
                "inputs": {"latent": motion_context_plan.checkpoint_input},
            }
            workflow["22"] = {
                "class_type": "MiniMaxH3MotionContext",
                "inputs": {
                    "conditioning": ["8", 0],
                    "vae": ["6", 0],
                    "latent": ["8", 1],
                    "context_length": str(motion_context_plan.video_frames),
                    "audio_context_length": motion_context_plan.audio_frames,
                    "context_latent": ["21", 0],
                },
            }
            workflow["10"]["inputs"]["conditioning"] = ["22", 0]
            workflow["23"] = {
                "class_type": "MiniMaxH3MotionContextTrim",
                "inputs": {
                    "images": ["14", 0],
                    "audio": ["15", 0],
                    "trim_frames": ["22", 1],
                    "fps": 24.0,
                    "match_tail": True,
                },
            }
            workflow["16"]["inputs"].update({"images": ["23", 0], "audio": ["23", 1]})
        if motion_context_plan.save_context:
            workflow["19"] = {
                "class_type": "H3StudioSaveLatent",
                "inputs": {
                    "samples": ["13", 0],
                    "video_done": ["17", 0],
                    "filename_prefix": f"h3-studio/motion-context/{job_id}",
                },
            }
    return workflow


def compile_image_workflow(spec: GenerationSpec, config: Config, job_id: str) -> dict[str, Any]:
    if spec.output_type != "image":
        raise ValueError("image spec required")
    checkpoint = spec.model_bindings.get("image_checkpoint", config.image_checkpoint)
    if not checkpoint:
        raise CapabilityError(
            "text-to-image is disabled: set H3_STUDIO_IMAGE_CHECKPOINT to an installed checkpoint"
        )
    negative = spec.negative_prompt or "low quality, blurry, distorted anatomy, watermark, text"
    workflow: dict[str, Any] = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": spec.prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": spec.seed,
                "steps": spec.steps,
                "cfg": spec.cfg,
                "sampler_name": "euler_ancestral",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": f"h3-studio/images/{job_id}"},
        },
    }
    if spec.mode == "image-to-image":
        if len(spec.references) != 1 or spec.references[0].kind != "image":
            raise CapabilityError("image-to-image requires one image reference")
        workflow.update(
            {
                "8": {"class_type": "LoadImage", "inputs": {"image": spec.references[0].comfy_path}},
                "9": {
                    "class_type": "ImageScale",
                    "inputs": {
                        "image": ["8", 0], "upscale_method": "lanczos",
                        "width": spec.width, "height": spec.height, "crop": "center",
                    },
                },
                "10": {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["1", 2]}},
            }
        )
        workflow["5"]["inputs"]["latent_image"] = ["10", 0]
        workflow["5"]["inputs"]["denoise"] = spec.denoise
    else:
        workflow["4"] = {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": spec.width, "height": spec.height, "batch_size": 1},
        }
        workflow["5"]["inputs"]["latent_image"] = ["4", 0]
    return workflow


def _flow_image_models(spec: GenerationSpec) -> tuple[str, str, str]:
    values = tuple(spec.model_bindings.get(role, "") for role in (
        "image_diffusion_model", "image_text_encoder", "image_vae",
    ))
    if not all(values):
        raise CapabilityError("image workflow profile is missing a diffusion model, text encoder, or VAE binding")
    return values


def compile_z_image_workflow(spec: GenerationSpec, job_id: str) -> dict[str, Any]:
    """Compile the profile-bound official ComfyUI Z-Image-Turbo sampling graph."""

    model, encoder, vae = _flow_image_models(spec)
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": encoder, "type": "lumina2", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": spec.prompt, "clip": ["3", 0]}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": spec.width, "height": spec.height, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["2", 0], "positive": ["5", 0], "negative": ["6", 0],
            "latent_image": ["7", 0], "seed": spec.seed, "steps": spec.steps,
            "cfg": spec.cfg, "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0,
        }},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["4", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": f"h3-studio/images/{job_id}"}},
    }


def compile_z_image_img2img_workflow(spec: GenerationSpec, job_id: str) -> dict[str, Any]:
    """Compile experimental single-image latent img2img for Z-Image Turbo.

    This is deliberately not represented as Z-Image-Edit: it VAE-encodes one
    image as the sampler's initial latent and offers no edit-model conditioning.
    """

    if spec.output_type != "image" or spec.compiler != "z_image_img2img":
        raise CapabilityError("Z-Image latent img2img compiler received an incompatible spec")
    if len(spec.references) != 1 or spec.references[0].kind != "image":
        raise CapabilityError("Z-Image latent img2img requires exactly one source image")
    if spec.steps != 8 or spec.cfg != 1:
        raise CapabilityError("Z-Image Turbo latent img2img requires 8 steps and CFG 1")
    if not math.isfinite(spec.denoise) or not 0.05 <= spec.denoise <= 1:
        raise CapabilityError("Z-Image latent img2img denoise must be between 0.05 and 1")

    model, encoder, vae = _flow_image_models(spec)
    for role, binding in (
        ("image_diffusion_model", model), ("image_text_encoder", encoder),
        ("image_vae", vae),
    ):
        path = PurePosixPath(binding)
        if (
            len(binding) > 240 or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in binding or any(ord(char) < 32 for char in binding)
        ):
            raise CapabilityError(f"Z-Image latent img2img profile has an unsafe {role} binding")

    source = spec.references[0]
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": model, "weight_dtype": "default",
        }},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {
            "model": ["1", 0], "shift": 3.0,
        }},
        "3": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": encoder, "type": "lumina2", "device": "default",
        }},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {
            "text": spec.prompt, "clip": ["3", 0],
        }},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {
            "conditioning": ["5", 0],
        }},
        "7": {"class_type": "LoadImage", "inputs": {"image": source.comfy_path}},
        "8": {"class_type": "ImageScale", "inputs": {
            "image": ["7", 0], "upscale_method": "lanczos",
            "width": spec.width, "height": spec.height, "crop": "center",
        }},
        "9": {"class_type": "VAEEncode", "inputs": {
            "pixels": ["8", 0], "vae": ["4", 0],
        }},
        "10": {"class_type": "KSampler", "inputs": {
            "model": ["2", 0], "positive": ["5", 0], "negative": ["6", 0],
            "latent_image": ["9", 0], "seed": spec.seed, "steps": 8, "cfg": 1.0,
            "sampler_name": "res_multistep", "scheduler": "simple", "denoise": spec.denoise,
        }},
        "11": {"class_type": "VAEDecode", "inputs": {
            "samples": ["10", 0], "vae": ["4", 0],
        }},
        "12": {"class_type": "SaveImage", "inputs": {
            "images": ["11", 0], "filename_prefix": f"h3-studio/images/{job_id}",
        }},
    }


def compile_z_image_lora_workflow(spec: GenerationSpec, job_id: str) -> dict[str, Any]:
    """Compile reviewed Z-Image Turbo + one bound model-only LoRA graphs."""

    if spec.output_type != "image":
        raise ValueError("image spec required")
    if spec.compiler not in {"z_image_lora_t2i", "z_image_lora_img2img"}:
        raise CapabilityError("Z-Image LoRA compiler received an untrusted compiler kind")
    if not math.isfinite(spec.lora_strength) or not 0 <= spec.lora_strength <= 2:
        raise CapabilityError("Z-Image LoRA strength must be between 0 and 2")
    expects_image = spec.compiler == "z_image_lora_img2img"
    expected_count = 1 if expects_image else 0
    if len(spec.references) != expected_count or any(ref.kind != "image" for ref in spec.references):
        raise CapabilityError(
            "Z-Image LoRA image-to-image requires exactly one source image"
            if expects_image else "Z-Image LoRA text-to-image does not accept source images"
        )
    model, encoder, vae = _flow_image_models(spec)
    lora = spec.image_lora
    if not lora:
        raise CapabilityError("Z-Image LoRA profile is missing its reviewed LoRA binding")
    if lora != spec.model_bindings.get("image_lora", ""):
        raise CapabilityError("Z-Image LoRA profile binding identity changed after request validation")
    for role, binding in (
        ("image_diffusion_model", model), ("image_text_encoder", encoder),
        ("image_vae", vae), ("image_lora", lora),
    ):
        path = PurePosixPath(binding)
        if (
            len(binding) > 240 or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in binding or any(ord(char) < 32 for char in binding)
        ):
            raise CapabilityError(f"Z-Image LoRA profile has an unsafe {role} binding")

    workflow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "2": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1", 0], "lora_name": lora, "strength_model": spec.lora_strength,
        }},
        "3": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["2", 0], "shift": 3.0}},
        "4": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": encoder, "type": "lumina2", "device": "default",
        }},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": spec.prompt, "clip": ["4", 0]}},
        "7": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["6", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["5", 0]}},
        "13": {"class_type": "SaveImage", "inputs": {
            "images": ["12", 0], "filename_prefix": f"h3-studio/images/{job_id}",
        }},
    }
    if expects_image:
        if not math.isfinite(spec.denoise) or not 0.05 <= spec.denoise <= 1:
            raise CapabilityError("Z-Image LoRA image-to-image denoise must be between 0.05 and 1")
        source = spec.references[0]
        workflow.update({
            "8": {"class_type": "LoadImage", "inputs": {"image": source.comfy_path}},
            "9": {"class_type": "ImageScale", "inputs": {
                "image": ["8", 0], "upscale_method": "lanczos",
                "width": spec.width, "height": spec.height, "crop": "center",
            }},
            "10": {"class_type": "VAEEncode", "inputs": {"pixels": ["9", 0], "vae": ["5", 0]}},
        })
        latent: list[Any] = ["10", 0]
        denoise = spec.denoise
    else:
        workflow["10"] = {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": spec.width, "height": spec.height, "batch_size": 1,
        }}
        latent = ["10", 0]
        denoise = 1.0
    workflow["11"] = {"class_type": "KSampler", "inputs": {
        "model": ["3", 0], "positive": ["6", 0], "negative": ["7", 0],
        "latent_image": latent, "seed": spec.seed, "steps": spec.steps,
        "cfg": spec.cfg, "sampler_name": "res_multistep", "scheduler": "simple",
        "denoise": denoise,
    }}
    return workflow


def compile_qwen_image_workflow(spec: GenerationSpec, job_id: str) -> dict[str, Any]:
    """Compile the official Qwen-Image 2512 base (50-step) graph."""

    model, encoder, vae = _flow_image_models(spec)
    negative = spec.negative_prompt or "low resolution, low quality, distorted anatomy, oversaturated, waxy skin, blurry text"
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.1}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": encoder, "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": spec.prompt, "clip": ["3", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["3", 0]}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": spec.width, "height": spec.height, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["2", 0], "positive": ["5", 0], "negative": ["6", 0],
            "latent_image": ["7", 0], "seed": spec.seed, "steps": spec.steps,
            "cfg": spec.cfg, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        }},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["4", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": f"h3-studio/images/{job_id}"}},
    }


def compile_qwen_edit_workflow(spec: GenerationSpec, job_id: str) -> dict[str, Any]:
    """Compile Qwen-Image-Edit 2511 with one instruction-conditioned source image."""

    if len(spec.references) != 1 or spec.references[0].kind != "image":
        raise CapabilityError("Qwen Image Edit requires exactly one source image")
    model, encoder, vae = _flow_image_models(spec)
    negative = spec.negative_prompt or " "
    source = spec.references[0]
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "2": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.1}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": encoder, "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "5": {"class_type": "LoadImage", "inputs": {"image": source.comfy_path}},
        "6": {"class_type": "ImageScale", "inputs": {
            "image": ["5", 0], "upscale_method": "lanczos", "width": spec.width,
            "height": spec.height, "crop": "center",
        }},
        "7": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
            "clip": ["3", 0], "vae": ["4", 0], "image1": ["6", 0], "prompt": spec.prompt,
        }},
        "8": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
            "clip": ["3", 0], "vae": ["4", 0], "image1": ["6", 0], "prompt": negative,
        }},
        "9": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
            "conditioning": ["7", 0], "reference_latents_method": "index_timestep_zero",
        }},
        "10": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
            "conditioning": ["8", 0], "reference_latents_method": "index_timestep_zero",
        }},
        "11": {"class_type": "VAEEncode", "inputs": {"pixels": ["6", 0], "vae": ["4", 0]}},
        "15": {"class_type": "CFGNorm", "inputs": {"model": ["2", 0], "strength": 1.0, "pre_cfg": False}},
        "12": {"class_type": "KSampler", "inputs": {
            "model": ["15", 0], "positive": ["9", 0], "negative": ["10", 0],
            "latent_image": ["11", 0], "seed": spec.seed, "steps": spec.steps,
            "cfg": spec.cfg, "sampler_name": "euler", "scheduler": "simple", "denoise": spec.denoise,
        }},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["4", 0]}},
        "14": {"class_type": "SaveImage", "inputs": {"images": ["13", 0], "filename_prefix": f"h3-studio/images/{job_id}"}},
    }


def compile_qwen_image_21_workflow(spec: GenerationSpec, job_id: str) -> dict[str, Any]:
    """Compile the native Qwen-Image 2.1 BF16 text and ordered image-edit paths."""

    if spec.output_type != "image" or len(spec.references) > 10 or any(ref.kind != "image" for ref in spec.references):
        raise CapabilityError("Qwen-Image 2.1 accepts text and up to ten image references")
    if spec.width % 32 or spec.height % 32:
        raise CapabilityError("Qwen-Image 2.1 width and height must be multiples of 32")
    model, encoder, vae = _flow_image_models(spec)
    workflow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": encoder, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "4": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "5": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": spec.prompt,
            "negative_prompt": spec.negative_prompt, "resolution": 0 if spec.references else 1024,
        }},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["4", 0], "positive": ["5", 0], "negative": ["5", 1],
            "latent_image": ["5", 2] if spec.references else ["6", 0],
            "seed": spec.seed, "steps": spec.steps, "cfg": spec.cfg,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": f"h3-studio/images/{job_id}"}},
    }
    if not spec.references:
        workflow["6"] = {"class_type": "EmptyLatentImage", "inputs": {
            "width": spec.width, "height": spec.height, "batch_size": 1,
        }}
    else:
        workflow["5"]["inputs"]["vae"] = ["3", 0]
        for index, reference in enumerate(spec.references, start=1):
            load_id, scale_id = str(100 + index * 2), str(101 + index * 2)
            workflow[load_id] = {"class_type": "LoadImage", "inputs": {"image": reference.comfy_path}}
            if index == 1:
                workflow[scale_id] = {"class_type": "ImageScale", "inputs": {
                    "image": [load_id, 0], "upscale_method": "lanczos",
                    "width": spec.width, "height": spec.height, "crop": "center",
                }}
            else:
                workflow[scale_id] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
                    "image": [load_id, 0], "upscale_method": "lanczos",
                    "megapixels": 1.0, "resolution_steps": 1,
                }}
            workflow["5"]["inputs"][f"images.image_{index}"] = [scale_id, 0]
    return workflow


def compile_flux2_klein_workflow(spec: GenerationSpec, job_id: str) -> dict[str, Any]:
    """Compile the native ComfyUI FLUX.2 Klein distilled graph.

    Reference images are encoded independently and chained in the exact order
    stored in ``spec.references``.  Both positive and zeroed-negative
    conditioning receive the same ordered latent sequence, matching ComfyUI's
    official single- and multi-reference blueprints.
    """

    if spec.output_type != "image":
        raise ValueError("image spec required")
    if len(spec.references) > 4 or any(reference.kind != "image" for reference in spec.references):
        raise CapabilityError("FLUX.2 Klein accepts zero to four ordered image references")
    if spec.steps != 4 or spec.cfg != 1:
        raise CapabilityError("FLUX.2 Klein distilled requires 4 steps and CFG 1")
    if spec.width % 16 or spec.height % 16:
        raise CapabilityError("FLUX.2 output dimensions must be multiples of 16")

    model, encoder, vae = _flow_image_models(spec)
    workflow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": model, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": encoder, "type": "flux2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": spec.prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {
            "width": spec.width, "height": spec.height, "batch_size": 1,
        }},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": spec.seed}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "Flux2Scheduler", "inputs": {
            "steps": 4, "width": spec.width, "height": spec.height,
        }},
        "10": {"class_type": "CFGGuider", "inputs": {
            "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "cfg": 1.0,
        }},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["7", 0], "guider": ["10", 0], "sampler": ["8", 0],
            "sigmas": ["9", 0], "latent_image": ["6", 0],
        }},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "SaveImage", "inputs": {
            "images": ["12", 0], "filename_prefix": f"h3-studio/images/{job_id}",
        }},
    }

    positive: list[Any] = ["4", 0]
    negative: list[Any] = ["5", 0]
    for index, reference in enumerate(spec.references):
        base = 100 + index * 5
        load_id, scale_id, encode_id = str(base), str(base + 1), str(base + 2)
        positive_id, negative_id = str(base + 3), str(base + 4)
        workflow[load_id] = {"class_type": "LoadImage", "inputs": {"image": reference.comfy_path}}
        scale_method = "lanczos" if "9b" in spec.profile_id.lower() and len(spec.references) > 1 else "nearest-exact"
        workflow[scale_id] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": [load_id, 0], "upscale_method": scale_method,
            "megapixels": 1.0, "resolution_steps": 1,
        }}
        workflow[encode_id] = {"class_type": "VAEEncode", "inputs": {
            "pixels": [scale_id, 0], "vae": ["3", 0],
        }}
        workflow[positive_id] = {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": positive, "latent": [encode_id, 0],
        }}
        workflow[negative_id] = {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": negative, "latent": [encode_id, 0],
        }}
        positive = [positive_id, 0]
        negative = [negative_id, 0]
    workflow["10"]["inputs"]["positive"] = positive
    workflow["10"]["inputs"]["negative"] = negative
    return workflow


def compile_workflow(
    spec: GenerationSpec, config: Config, job_id: str,
    resume_plan: ResumeSamplingPlan | None = None,
    motion_context_plan: MotionContextPlan | None = None,
) -> dict[str, Any]:
    if spec.compiler not in {
        "h3_fl", "h3_ref", "checkpoint_t2i", "checkpoint_img2img",
        "z_image_t2i", "z_image_img2img", "z_image_lora_t2i", "z_image_lora_img2img",
        "qwen_image_t2i", "qwen_image_edit", "qwen_image_21", "flux2_klein",
    }:
        raise CapabilityError("workflow profile selected an unsupported compiler")
    if spec.compiler in {"h3_fl", "h3_ref"}:
        return compile_video_workflow(spec, config, job_id, resume_plan, motion_context_plan)
    if spec.compiler == "z_image_t2i":
        return compile_z_image_workflow(spec, job_id)
    if spec.compiler == "z_image_img2img":
        return compile_z_image_img2img_workflow(spec, job_id)
    if spec.compiler in {"z_image_lora_t2i", "z_image_lora_img2img"}:
        return compile_z_image_lora_workflow(spec, job_id)
    if spec.compiler == "qwen_image_t2i":
        return compile_qwen_image_workflow(spec, job_id)
    if spec.compiler == "qwen_image_edit":
        return compile_qwen_edit_workflow(spec, job_id)
    if spec.compiler == "qwen_image_21":
        return compile_qwen_image_21_workflow(spec, job_id)
    if spec.compiler == "flux2_klein":
        return compile_flux2_klein_workflow(spec, job_id)
    return compile_image_workflow(spec, config, job_id)


def workflow_evidence(workflow: dict[str, Any], spec: GenerationSpec, job_id: str) -> dict[str, Any]:
    """Summarize the actual compiled graph, not merely requested parameters."""

    def node_inputs(class_type: str) -> dict[str, Any]:
        node = next(
            (
                item for item in workflow.values()
                if isinstance(item, dict) and item.get("class_type") == class_type
            ),
            None,
        )
        inputs = node.get("inputs") if isinstance(node, dict) else None
        return inputs if isinstance(inputs, dict) else {}

    lora = node_inputs("LoraLoaderModelOnly")
    basic_scheduler = node_inputs("BasicScheduler")
    flux2_scheduler = node_inputs("Flux2Scheduler")
    sampler_select = node_inputs("KSamplerSelect")
    ksampler = node_inputs("KSampler")
    random_noise = node_inputs("RandomNoise")
    split_sigmas = [
        node.get("inputs", {}) for node in workflow.values()
        if isinstance(node, dict) and node.get("class_type") == "SplitSigmas"
    ]
    h3_conditioning = node_inputs("MiniMaxH3ReferenceToVideo") or node_inputs("MiniMaxH3ImageToVideo")
    motion_context = node_inputs("MiniMaxH3MotionContext")
    compiled_prompt = h3_conditioning.get("prompt")
    if not isinstance(compiled_prompt, str) and spec.output_type == "image":
        if spec.compiler == "qwen_image_21":
            compiled_prompt = node_inputs("TextEncodeQwenImage21").get("prompt")
        elif spec.compiler == "qwen_image_edit":
            compiled_prompt = node_inputs("TextEncodeQwenImageEditPlus").get("prompt")
        else:
            compiled_prompt = node_inputs("CLIPTextEncode").get("text")
    prompt_sha256 = (
        hashlib.sha256(compiled_prompt.encode("utf-8")).hexdigest()
        if isinstance(compiled_prompt, str) else None
    )
    model = node_inputs("UNETLoader") or node_inputs("CheckpointLoaderSimple")
    return {
        "path": f"evidence/workflows/{job_id}.json",
        "director_mode": spec.director_mode or None,
        "resolved_director_mode": spec.director_mode or None,
        "source_asset_id": spec.source_asset_id or None,
        "source_video_tag": "<Video 1>" if spec.source_asset_id else None,
        "audio_output": "generated" if spec.output_type == "video" else None,
        "unsupported_audio_modes": ["source", "mute"] if spec.output_type == "video" else [],
        "node_classes": sorted(
            {str(node.get("class_type", "")) for node in workflow.values() if isinstance(node, dict)}
        ),
        "diffusion_model": model.get("unet_name", model.get("ckpt_name")),
        "lora": lora.get("lora_name"),
        "lora_strength": lora.get("strength_model", 0),
        "image_lora": lora.get("lora_name") if spec.output_type == "image" else None,
        "image_lora_strength": lora.get("strength_model", 0) if spec.output_type == "image" else None,
        "steps": spec.steps if node_inputs("SaveLatent") else basic_scheduler.get("steps", flux2_scheduler.get("steps", ksampler.get("steps", spec.steps))),
        "scheduler_total_steps": basic_scheduler.get("steps") if node_inputs("SaveLatent") else None,
        "sampler": sampler_select.get("sampler_name", ksampler.get("sampler_name", spec.sampler)),
        "scheduler": basic_scheduler.get("scheduler", "flux2" if flux2_scheduler else ksampler.get("scheduler", spec.scheduler)),
        "denoise": None if spec.compiler == "flux2_klein" else basic_scheduler.get("denoise", ksampler.get("denoise", spec.denoise)),
        "seed": random_noise.get("noise_seed", ksampler.get("seed", spec.seed)),
        "width": h3_conditioning.get("width", spec.width),
        "height": h3_conditioning.get("height", spec.height),
        "frames": h3_conditioning.get("length", spec.frames),
        "prompt_sha256": prompt_sha256,
        "guidance": "BasicGuider" if spec.output_type == "video" else None,
        "sigma_shifts": {"video": 12.0, "audio": 3.0} if spec.output_type == "video" else None,
        "resumable_sampling": bool(node_inputs("SaveLatent")),
        "resume_from_checkpoint": bool(node_inputs("LoadLatent")),
        "sigma_splits": [item.get("step") for item in split_sigmas if isinstance(item, dict)],
        "motion_context": {
            "enabled": bool(motion_context),
            "video_frames": int(motion_context.get("context_length", 0) or 0),
            "audio_frames": int(motion_context.get("audio_context_length", 0) or 0),
            "trimmed": bool(node_inputs("MiniMaxH3MotionContextTrim")),
            "context_saved": bool(node_inputs("H3StudioSaveLatent")),
        },
    }
