"""Safe, versioned workflow profile registry.

Profiles describe capabilities and parameters.  They deliberately do not contain
raw ComfyUI graphs: a manifest may only select one of the reviewed compiler kinds
below, so dropping a JSON file into the profiles directory cannot execute an
arbitrary node or read an arbitrary model path.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import ApiError


ALLOWED_COMPILERS = frozenset({
    "h3_fl", "h3_ref", "checkpoint_t2i", "checkpoint_img2img",
    "z_image_t2i", "z_image_img2img", "z_image_lora_t2i", "z_image_lora_img2img",
    "qwen_image_t2i", "qwen_image_edit", "qwen_image_21", "flux2_klein",
})
ALLOWED_MODALITIES = frozenset({"text", "image", "video", "audio"})
ALLOWED_MODEL_ROLES = frozenset(
    {
        "fl_model", "ref_model", "text_encoder", "video_vae", "audio_vae",
        "fl_lora", "ref_lora", "image_checkpoint", "image_diffusion_model",
        "image_text_encoder", "image_vae", "image_lora",
    }
)
H3_MAX_FRAMES = 362
H3_MAX_DURATION_SECONDS = H3_MAX_FRAMES / 24
H3_MAX_REFERENCES = 12
H3_REFERENCE_CAPACITY = {"image": 9, "video": 3, "audio": 3}


@dataclass(frozen=True, slots=True)
class WorkflowProfile:
    id: str
    version: str
    display_name: str
    output_type: str
    input_modalities: tuple[str, ...]
    required_nodes: tuple[str, ...]
    required_models: tuple[str, ...]
    parameter_schema: dict[str, Any]
    defaults: dict[str, Any]
    limits: dict[str, Any]
    compiler: str
    sampling_mode: str = "default"
    model_bindings: dict[str, str] = field(default_factory=dict)
    resume: dict[str, Any] = field(default_factory=dict)
    built_in: bool = False
    license_id: str = ""
    license_url: str = ""
    use_notice: str = ""
    compatible_identities: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def digest(self) -> str:
        canonical = {
            "id": self.id,
            "version": self.version,
            "display_name": self.display_name,
            "output_type": self.output_type,
            "input_modalities": self.input_modalities,
            "required_nodes": self.required_nodes,
            "required_models": self.required_models,
            "parameter_schema": self.parameter_schema,
            "defaults": self.defaults,
            "limits": self.limits,
            "compiler": self.compiler,
            "sampling_mode": self.sampling_mode,
            "model_bindings": self.model_bindings,
        }
        if self.resume:
            canonical["resume"] = self.resume
        # Optional descriptive metadata participates in a profile identity only
        # when it exists. This preserves every pre-license profile digest and
        # keeps previously persisted jobs/projects valid across the upgrade.
        canonical.update({
            key: value for key, value in (
                ("license_id", self.license_id),
                ("license_url", self.license_url),
                ("use_notice", self.use_notice),
            ) if value
        })
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def public(self) -> dict[str, Any]:
        reference_max = self.limits.get("references", 0)
        reference_contract: dict[str, Any] = {
            "media_types": [kind for kind in self.input_modalities if kind != "text"],
            "min_count": 0,
            "max_count": reference_max if isinstance(reference_max, int) else 0,
            "ordered": False,
        }
        if self.compiler in {
            "checkpoint_img2img", "z_image_img2img", "z_image_lora_img2img", "qwen_image_edit",
        }:
            reference_contract.update({"min_count": 1, "ordered": False})
        elif self.compiler == "h3_ref":
            reference_contract.update({"min_count": 1, "ordered": True})
        elif self.compiler == "flux2_klein":
            reference_contract.update({
                "min_count": 0,
                "max_count": reference_max if isinstance(reference_max, int) else 0,
                "ordered": True,
                "order_field": "reference_index",
                "index_base": 0,
                "prompt_reference_format": "image {n}",
                "prompt_index_base": 1,
                "roles": ["reference", "init_image", "image_edit"],
            })
        elif self.compiler == "qwen_image_21":
            reference_contract.update({
                "min_count": 0,
                "max_count": 10,
                "ordered": True,
                "order_field": "reference_index",
                "index_base": 0,
                "prompt_reference_format": "<image{n}>",
                "prompt_index_base": 1,
                "roles": ["reference", "init_image", "image_edit"],
            })
        return {
            "id": self.id,
            "version": self.version,
            "display_name": self.display_name,
            "output_type": self.output_type,
            "input_modalities": list(self.input_modalities),
            "required_nodes": list(self.required_nodes),
            "required_models": list(self.required_models),
            "parameter_schema": self.parameter_schema,
            "defaults": self.defaults,
            "limits": self.limits,
            "compiler": self.compiler,
            "sampling_mode": self.sampling_mode,
            "model_bindings": self.model_bindings,
            "built_in": self.built_in,
            "license_id": self.license_id or None,
            "license_url": self.license_url or None,
            "use_notice": self.use_notice or None,
            "reference_contract": reference_contract,
            "resume": self.resume or {"supported": False, "reason": "profile_not_tested"},
            "compatible_identities": [
                {"version": version, "manifest_sha256": digest}
                for version, digest in self.compatible_identities
            ],
            "manifest_sha256": self.digest(),
        }

    def accepts_identity(self, version: str, digest: str) -> bool:
        """Accept the current profile identity plus explicitly reviewed predecessors."""
        return (version == self.version and digest == self.digest()) or (version, digest) in self.compatible_identities

    def accepts_version(self, version: str) -> bool:
        return version == self.version or any(candidate == version for candidate, _digest in self.compatible_identities)

    def accepts_digest(self, digest: str) -> bool:
        return digest == self.digest() or any(candidate == digest for _version, candidate in self.compatible_identities)


VIDEO_SHARED = (
    "UNETLoader", "CLIPLoader", "VAELoader",
    "PathchSageAttentionKJ", "MiniMaxH3MemoryEfficientSageAttentionPatch",
    "RandomNoise", "BasicGuider", "KSamplerSelect", "BasicScheduler",
    "SamplerCustomAdvanced", "VAEDecode", "VAEDecodeAudio", "CreateVideo", "SaveVideo",
)
VIDEO_RESUME_NODES = (
    "SplitSigmas", "H3StudioSaveLatent", "H3StudioLoadLatent", "DisableNoise",
)
IMAGE_SHARED = ("CheckpointLoaderSimple", "CLIPTextEncode", "KSampler", "VAEDecode", "SaveImage")
FLOW_IMAGE_SHARED = (
    "UNETLoader", "CLIPLoader", "VAELoader", "ModelSamplingAuraFlow",
    "KSampler", "VAEDecode", "SaveImage",
)
FLUX2_KLEIN_SHARED = (
    "UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode",
    "ConditioningZeroOut", "LoadImage", "ImageScaleToTotalPixels", "VAEEncode",
    "ReferenceLatent", "EmptyFlux2LatentImage", "RandomNoise", "CFGGuider",
    "KSamplerSelect", "Flux2Scheduler", "SamplerCustomAdvanced", "VAEDecode",
    "SaveImage",
)

# A downloaded manifest may add declarative checks, but it may never remove the
# dependencies used by its reviewed compiler.  This keeps capability discovery
# honest: `available=true` means the compiler's actual graph can run.
COMPILER_BASELINES: dict[str, dict[str, Any]] = {
    "h3_fl": {
        "output_type": "video",
        "modalities": ("text", "image"),
        "nodes": VIDEO_SHARED + ("MiniMaxH3ImageToVideo", "LoadImage"),
        "models": ("fl_model", "text_encoder", "video_vae", "audio_vae"),
    },
    "h3_ref": {
        "output_type": "video",
        "modalities": ("text", "image", "video", "audio"),
        "nodes": VIDEO_SHARED + ("MiniMaxH3ReferenceToVideo", "LoadImage", "LoadVideo", "LoadAudio", "GetVideoComponents"),
        "models": ("ref_model", "text_encoder", "video_vae", "audio_vae"),
    },
    "checkpoint_t2i": {
        "output_type": "image",
        "modalities": ("text",),
        "nodes": IMAGE_SHARED + ("EmptyLatentImage",),
        "models": ("image_checkpoint",),
    },
    "checkpoint_img2img": {
        "output_type": "image",
        "modalities": ("text", "image"),
        "nodes": IMAGE_SHARED + ("LoadImage", "ImageScale", "VAEEncode"),
        "models": ("image_checkpoint",),
    },
    "z_image_t2i": {
        "output_type": "image",
        "modalities": ("text",),
        "nodes": FLOW_IMAGE_SHARED + ("CLIPTextEncode", "ConditioningZeroOut", "EmptySD3LatentImage"),
        "models": ("image_diffusion_model", "image_text_encoder", "image_vae"),
    },
    "z_image_img2img": {
        "output_type": "image",
        "modalities": ("text", "image"),
        "nodes": FLOW_IMAGE_SHARED + (
            "CLIPTextEncode", "ConditioningZeroOut", "LoadImage", "ImageScale", "VAEEncode",
        ),
        "models": ("image_diffusion_model", "image_text_encoder", "image_vae"),
    },
    "z_image_lora_t2i": {
        "output_type": "image",
        "modalities": ("text",),
        "nodes": FLOW_IMAGE_SHARED + (
            "LoraLoaderModelOnly", "CLIPTextEncode", "ConditioningZeroOut",
            "EmptySD3LatentImage",
        ),
        "models": ("image_diffusion_model", "image_text_encoder", "image_vae", "image_lora"),
    },
    "z_image_lora_img2img": {
        "output_type": "image",
        "modalities": ("text", "image"),
        "nodes": FLOW_IMAGE_SHARED + (
            "LoraLoaderModelOnly", "CLIPTextEncode", "ConditioningZeroOut",
            "LoadImage", "ImageScale", "VAEEncode",
        ),
        "models": ("image_diffusion_model", "image_text_encoder", "image_vae", "image_lora"),
    },
    "qwen_image_t2i": {
        "output_type": "image",
        "modalities": ("text",),
        "nodes": FLOW_IMAGE_SHARED + ("CLIPTextEncode", "EmptySD3LatentImage"),
        "models": ("image_diffusion_model", "image_text_encoder", "image_vae"),
    },
    "qwen_image_edit": {
        "output_type": "image",
        "modalities": ("text", "image"),
        "nodes": FLOW_IMAGE_SHARED + (
            "LoadImage", "ImageScale", "VAEEncode", "TextEncodeQwenImageEditPlus",
            "FluxKontextMultiReferenceLatentMethod", "CFGNorm",
        ),
        "models": ("image_diffusion_model", "image_text_encoder", "image_vae"),
    },
    "qwen_image_21": {
        "output_type": "image",
        "modalities": ("text", "image"),
        "nodes": (
            "UNETLoader", "CLIPLoader", "VAELoader", "TextEncodeQwenImage21",
            "QwenImage21Cache", "EmptyLatentImage", "LoadImage", "ImageScale",
            "ImageScaleToTotalPixels", "KSampler", "VAEDecode", "SaveImage",
        ),
        "models": ("image_diffusion_model", "image_text_encoder", "image_vae"),
    },
    "flux2_klein": {
        "output_type": "image",
        "modalities": ("text", "image"),
        "nodes": FLUX2_KLEIN_SHARED,
        "models": ("image_diffusion_model", "image_text_encoder", "image_vae"),
    },
}

COMPILER_PARAMETERS: dict[str, dict[str, Any]] = {
    "h3_fl": {
        "schema": {"duration": "number", "width": "integer", "height": "integer", "steps": "integer", "lora_strength": "number", "denoise": "number", "seed": "integer"},
        "defaults": {"duration": 124 / 24, "steps": 20, "lora_strength": 0, "denoise": 1.0},
        "limits": {"duration": [5, H3_MAX_DURATION_SECONDS], "references": 2, "steps": [4, 50], "lora_strength": [0, 2], "denoise": [0.05, 1]},
    },
    "h3_ref": {
        "schema": {"duration": "number", "width": "integer", "height": "integer", "steps": "integer", "lora_strength": "number", "ref_image_size": "string", "denoise": "number", "seed": "integer"},
        "defaults": {"duration": 124 / 24, "steps": 20, "lora_strength": 0, "ref_image_size": "match", "denoise": 1.0},
        "limits": {"duration": [5, H3_MAX_DURATION_SECONDS], "references": H3_MAX_REFERENCES, "steps": [4, 50], "lora_strength": [0, 2], "denoise": [0.05, 1]},
    },
    "checkpoint_t2i": {
        "schema": {"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        "defaults": {"steps": 24, "cfg": 7},
        "limits": {"references": 0, "steps": [1, 100], "cfg": [1, 30]},
    },
    "checkpoint_img2img": {
        "schema": {"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "denoise": "number", "seed": "integer"},
        "defaults": {"steps": 24, "cfg": 7, "denoise": 0.65},
        "limits": {"references": 1, "steps": [1, 100], "cfg": [1, 30], "denoise": [0.05, 1]},
    },
    "z_image_t2i": {
        "schema": {"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        "defaults": {"steps": 8, "cfg": 1},
        "limits": {"references": 0, "steps": [8, 8], "cfg": [1, 1]},
    },
    "z_image_img2img": {
        "schema": {
            "width": "integer", "height": "integer", "steps": "integer",
            "cfg": "number", "denoise": "number", "seed": "integer",
        },
        "defaults": {"steps": 8, "cfg": 1, "denoise": 0.65},
        "limits": {
            "references": 1, "steps": [8, 8], "cfg": [1, 1], "denoise": [0.05, 1],
        },
    },
    "z_image_lora_t2i": {
        "schema": {
            "width": "integer", "height": "integer", "steps": "integer",
            "cfg": "number", "lora_strength": "number", "seed": "integer",
        },
        "defaults": {"steps": 8, "cfg": 1, "lora_strength": 1.0},
        "limits": {
            "references": 0, "steps": [8, 8], "cfg": [1, 1],
            "lora_strength": [0, 2],
        },
    },
    "z_image_lora_img2img": {
        "schema": {
            "width": "integer", "height": "integer", "steps": "integer",
            "cfg": "number", "lora_strength": "number", "denoise": "number",
            "seed": "integer",
        },
        "defaults": {"steps": 8, "cfg": 1, "lora_strength": 1.0, "denoise": 0.65},
        "limits": {
            "references": 1, "steps": [8, 8], "cfg": [1, 1],
            "lora_strength": [0, 2], "denoise": [0.05, 1],
        },
    },
    "qwen_image_t2i": {
        "schema": {"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        "defaults": {"steps": 50, "cfg": 4},
        "limits": {"references": 0, "steps": [20, 60], "cfg": [1, 10]},
    },
    "qwen_image_edit": {
        "schema": {"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "denoise": "number", "seed": "integer"},
        "defaults": {"steps": 40, "cfg": 3, "denoise": 1},
        "limits": {"references": 1, "steps": [20, 60], "cfg": [1, 10], "denoise": [0.05, 1]},
    },
    "qwen_image_21": {
        "schema": {"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        "defaults": {"steps": 40, "cfg": 1},
        "limits": {"references": 10, "steps": [1, 60], "cfg": [1, 10]},
    },
    "flux2_klein": {
        "schema": {"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        "defaults": {"steps": 4, "cfg": 1},
        "limits": {"references": 4, "steps": [4, 4], "cfg": [1, 1]},
    },
}


def _profile(**value: Any) -> WorkflowProfile:
    return WorkflowProfile(built_in=True, **value)


BUILTIN_PROFILES = (
    _profile(
        id="minimax-h3-fl2va", version="1.2", display_name="MiniMax H3 FL2VA · Turbo LoRA（4 步推荐）",
        output_type="video", input_modalities=("text", "image"),
        required_nodes=VIDEO_SHARED + ("LoraLoaderModelOnly", "MiniMaxH3ImageToVideo", "LoadImage"),
        required_models=("fl_model", "text_encoder", "video_vae", "audio_vae", "fl_lora"),
        parameter_schema={"duration": "number", "width": "integer", "height": "integer", "steps": "integer", "lora_strength": "number", "denoise": "number", "seed": "integer"},
        defaults={"duration": 124 / 24, "steps": 4, "lora_strength": 0.75, "denoise": 1.0},
        limits={"duration": [5, H3_MAX_DURATION_SECONDS], "references": 2, "steps": [4, 50], "lora_strength": [0, 2], "denoise": [0.05, 1]}, compiler="h3_fl", sampling_mode="turbo4",
    ),
    _profile(
        id="minimax-h3-fl2va-base", version="1.2", display_name="MiniMax H3 FL2VA · Base 20 Direct (no Turbo)",
        output_type="video", input_modalities=("text", "image"),
        required_nodes=VIDEO_SHARED + ("MiniMaxH3ImageToVideo", "LoadImage"),
        required_models=("fl_model", "text_encoder", "video_vae", "audio_vae"),
        parameter_schema={"duration": "number", "width": "integer", "height": "integer", "steps": "integer", "denoise": "number", "seed": "integer"},
        defaults={"duration": 124 / 24, "steps": 20, "denoise": 1.0},
        limits={"duration": [5, H3_MAX_DURATION_SECONDS], "references": 2, "steps": [4, 50], "denoise": [0.05, 1]}, compiler="h3_fl", sampling_mode="base",
        use_notice="Default Base profile: render the video directly; no checkpoint is placed on the critical path.",
    ),
    _profile(
        id="minimax-h3-fl2va-base-resumable", version="1.0", display_name="MiniMax H3 FL2VA · Base Resumable (opt-in)",
        output_type="video", input_modalities=("text", "image"),
        required_nodes=VIDEO_SHARED + VIDEO_RESUME_NODES + ("MiniMaxH3ImageToVideo", "LoadImage"),
        required_models=("fl_model", "text_encoder", "video_vae", "audio_vae"),
        parameter_schema={"duration": "number", "width": "integer", "height": "integer", "steps": "integer", "denoise": "number", "seed": "integer"},
        defaults={"duration": 124 / 24, "steps": 20, "denoise": 1.0},
        limits={"duration": [5, H3_MAX_DURATION_SECONDS], "references": 2, "steps": [4, 50], "denoise": [0.05, 1]}, compiler="h3_fl", sampling_mode="base",
        resume={"supported": True, "schedule_version": "h3-simple-fixed/v1", "max_total_steps": 50, "additional_steps": [1, 46]},
        use_notice="Opt-in continuation profile; requires the bundled H3 Studio checkpoint nodes in ComfyUI.",
    ),
    _profile(
        id="minimax-h3-ref2va", version="1.3", display_name="MiniMax H3 Ref2VA · Turbo LoRA（4 步推荐）",
        output_type="video", input_modalities=("text", "image", "video", "audio"),
        required_nodes=VIDEO_SHARED + ("LoraLoaderModelOnly", "MiniMaxH3ReferenceToVideo", "LoadImage", "LoadVideo", "LoadAudio", "GetVideoComponents"),
        required_models=("ref_model", "text_encoder", "video_vae", "audio_vae", "ref_lora"),
        parameter_schema={"duration": "number", "width": "integer", "height": "integer", "steps": "integer", "lora_strength": "number", "ref_image_size": "string", "denoise": "number", "seed": "integer"},
        defaults={"duration": 124 / 24, "steps": 4, "lora_strength": 0.75, "ref_image_size": "match", "denoise": 1.0},
        limits={"duration": [5, H3_MAX_DURATION_SECONDS], "references": H3_MAX_REFERENCES, "steps": [4, 50], "lora_strength": [0, 2], "denoise": [0.05, 1]}, compiler="h3_ref", sampling_mode="turbo4",
        compatible_identities=(("1.2", "d961eecd308a42dcf9730c1853dba9b8213284d69d75878d6865f5da1fd1465d"),),
    ),
    _profile(
        id="minimax-h3-ref2va-base", version="1.3", display_name="MiniMax H3 Ref2VA · Base 20 Direct (no Turbo)",
        output_type="video", input_modalities=("text", "image", "video", "audio"),
        required_nodes=VIDEO_SHARED + ("MiniMaxH3ReferenceToVideo", "LoadImage", "LoadVideo", "LoadAudio", "GetVideoComponents"),
        required_models=("ref_model", "text_encoder", "video_vae", "audio_vae"),
        parameter_schema={"duration": "number", "width": "integer", "height": "integer", "steps": "integer", "ref_image_size": "string", "denoise": "number", "seed": "integer"},
        defaults={"duration": 124 / 24, "steps": 20, "ref_image_size": "match", "denoise": 1.0},
        limits={"duration": [5, H3_MAX_DURATION_SECONDS], "references": H3_MAX_REFERENCES, "steps": [4, 50], "denoise": [0.05, 1]}, compiler="h3_ref", sampling_mode="base",
        compatible_identities=(("1.2", "71bb15b0a310d743ed777dacd5c7d5e1ccc5ca2d943e3eea9deb2e800800b53c"),),
        use_notice="Default Base profile: render the video directly; no checkpoint is placed on the critical path.",
    ),
    _profile(
        id="minimax-h3-ref2va-base-resumable", version="1.1", display_name="MiniMax H3 Ref2VA · Base Resumable (opt-in)",
        output_type="video", input_modalities=("text", "image", "video", "audio"),
        required_nodes=VIDEO_SHARED + VIDEO_RESUME_NODES + ("MiniMaxH3ReferenceToVideo", "LoadImage", "LoadVideo", "LoadAudio", "GetVideoComponents"),
        required_models=("ref_model", "text_encoder", "video_vae", "audio_vae"),
        parameter_schema={"duration": "number", "width": "integer", "height": "integer", "steps": "integer", "ref_image_size": "string", "denoise": "number", "seed": "integer"},
        defaults={"duration": 124 / 24, "steps": 20, "ref_image_size": "match", "denoise": 1.0},
        limits={"duration": [5, H3_MAX_DURATION_SECONDS], "references": H3_MAX_REFERENCES, "steps": [4, 50], "denoise": [0.05, 1]}, compiler="h3_ref", sampling_mode="base",
        compatible_identities=(("1.0", "451682da20ceb18f6d0f4ed08a0e7699625348b74e99bfd6d08d92620c7ce49b"),),
        resume={"supported": True, "schedule_version": "h3-simple-fixed/v1", "max_total_steps": 50, "additional_steps": [1, 46]},
        use_notice="Opt-in continuation profile; requires the bundled H3 Studio checkpoint nodes in ComfyUI.",
    ),
    _profile(
        id="z-image-turbo-bf16-t2i", version="1.0",
        display_name="Z-Image Turbo BF16 · 高画质默认 / 中英文字",
        output_type="image", input_modalities=("text",),
        required_nodes=FLOW_IMAGE_SHARED + (
            "CLIPTextEncode", "ConditioningZeroOut", "EmptySD3LatentImage",
        ),
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae"),
        parameter_schema={
            "width": "integer", "height": "integer", "steps": "integer",
            "cfg": "number", "seed": "integer",
        },
        defaults={"steps": 8, "cfg": 1},
        limits={"references": 0, "steps": [8, 8], "cfg": [1, 1]},
        compiler="z_image_t2i",
        license_id="Apache-2.0",
        license_url="https://huggingface.co/Comfy-Org/z_image_turbo",
        use_notice=(
            "默认高画质 Profile：BF16 主模型 + 完整 Qwen3-4B 文本编码器。"
            "INT8 Profile 仅用于更低显存或更高并发。"
        ),
        model_bindings={
            "image_diffusion_model": "z_image_turbo_bf16.safetensors",
            "image_text_encoder": "qwen_3_4b.safetensors",
            "image_vae": "ae.safetensors",
        },
    ),
    _profile(
        id="z-image-turbo-bf16-img2img", version="1.0",
        display_name=(
            "Z-Image Turbo BF16 · 高画质实验性 latent 图生图"
            "（非官方 Z-Image Edit/模板）"
        ),
        output_type="image", input_modalities=("text", "image"),
        required_nodes=FLOW_IMAGE_SHARED + (
            "CLIPTextEncode", "ConditioningZeroOut", "LoadImage", "ImageScale", "VAEEncode",
        ),
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae"),
        parameter_schema={
            "width": "integer", "height": "integer", "steps": "integer",
            "cfg": "number", "denoise": "number", "seed": "integer",
        },
        defaults={"steps": 8, "cfg": 1, "denoise": 0.65},
        limits={
            "references": 1, "steps": [8, 8], "cfg": [1, 1], "denoise": [0.05, 1],
        },
        compiler="z_image_img2img",
        license_id="Apache-2.0",
        license_url="https://huggingface.co/Comfy-Org/z_image_turbo",
        use_notice=(
            "默认高画质 Profile：BF16 主模型 + 完整 Qwen3-4B 文本编码器。"
            "实验性 latent img2img，非官方 Z-Image Edit；"
            "denoise 默认 0.65，推荐 0.35–0.80，技术边界 0.05–1。"
        ),
        model_bindings={
            "image_diffusion_model": "z_image_turbo_bf16.safetensors",
            "image_text_encoder": "qwen_3_4b.safetensors",
            "image_vae": "ae.safetensors",
        },
    ),
    _profile(
        id="z-image-turbo-int8-t2i", version="1.0", display_name="Z-Image Turbo INT8 · 快速写实 / 中英文字",
        output_type="image", input_modalities=("text",),
        required_nodes=FLOW_IMAGE_SHARED + ("CLIPTextEncode", "ConditioningZeroOut", "EmptySD3LatentImage"),
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae"),
        parameter_schema={"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        defaults={"steps": 8, "cfg": 1},
        limits={"references": 0, "steps": [8, 8], "cfg": [1, 1]},
        compiler="z_image_t2i",
        model_bindings={
            "image_diffusion_model": "z_image_turbo_int8_convrot.safetensors",
            "image_text_encoder": "qwen_3_4b_fp8_mixed.safetensors",
            "image_vae": "ae.safetensors",
        },
    ),
    _profile(
        id="z-image-turbo-int8-img2img", version="1.0",
        display_name=(
            "Z-Image Turbo INT8 · 实验性 latent 图生图"
            "（非官方 Z-Image Edit/模板）"
        ),
        output_type="image", input_modalities=("text", "image"),
        required_nodes=FLOW_IMAGE_SHARED + (
            "CLIPTextEncode", "ConditioningZeroOut", "LoadImage", "ImageScale", "VAEEncode",
        ),
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae"),
        parameter_schema={
            "width": "integer", "height": "integer", "steps": "integer",
            "cfg": "number", "denoise": "number", "seed": "integer",
        },
        defaults={"steps": 8, "cfg": 1, "denoise": 0.65},
        limits={
            "references": 1, "steps": [8, 8], "cfg": [1, 1], "denoise": [0.05, 1],
        },
        compiler="z_image_img2img",
        use_notice=(
            "实验性 latent img2img：仅将单张图像编码为初始 latent。"
            "非官方 Z-Image Edit，亦非官方模板；"
            "denoise 默认 0.65，推荐 0.35–0.80，技术边界 0.05–1。"
        ),
        model_bindings={
            "image_diffusion_model": "z_image_turbo_int8_convrot.safetensors",
            "image_text_encoder": "qwen_3_4b_fp8_mixed.safetensors",
            "image_vae": "ae.safetensors",
        },
    ),
    _profile(
        id="z-image-turbo-zit-nsfw-t2i", version="1.1",
        display_name="Z-Image Turbo BF16 + ZIT NSFW LoRA · 文生图",
        output_type="image", input_modalities=("text",),
        required_nodes=FLOW_IMAGE_SHARED + (
            "LoraLoaderModelOnly", "CLIPTextEncode", "ConditioningZeroOut",
            "EmptySD3LatentImage",
        ),
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae", "image_lora"),
        parameter_schema={
            "width": "integer", "height": "integer", "steps": "integer",
            "cfg": "number", "lora_strength": "number", "seed": "integer",
        },
        defaults={"steps": 8, "cfg": 1, "lora_strength": 1.0},
        limits={
            "references": 0, "steps": [8, 8], "cfg": [1, 1],
            "lora_strength": [0, 1.25],
        },
        compiler="z_image_lora_t2i",
        license_id="Civitai Restricted License",
        license_url="https://civitai.com/models/2279079?modelVersionId=2565112",
        use_notice=(
            "BF16 主模型 + 完整 Qwen3-4B 文本编码器。"
            "社区文件 ZITnsfwLoRA.safetensors，SHA256 "
            "44bf34ce695ebcec6ca17f7dc27511f8fc4204943114d6c7c41cd4559e75dbaf；"
            "Civitai 元数据 allowCommercialUse=RentCivit、allowDerivatives=false。"
            "本地按非商业处理，禁止二次分发和衍生训练；仅限合法、自愿成年人内容。"
        ),
        model_bindings={
            "image_diffusion_model": "z_image_turbo_bf16.safetensors",
            "image_text_encoder": "qwen_3_4b.safetensors",
            "image_vae": "ae.safetensors",
            "image_lora": "ZITnsfwLoRA.safetensors",
        },
    ),
    _profile(
        id="z-image-turbo-zit-nsfw-img2img", version="1.1",
        display_name=(
            "Z-Image Turbo BF16 + ZIT NSFW LoRA · "
            "实验性 latent 图生图（非官方 Z-Image Edit/模板）"
        ),
        output_type="image", input_modalities=("text", "image"),
        required_nodes=FLOW_IMAGE_SHARED + (
            "LoraLoaderModelOnly", "CLIPTextEncode", "ConditioningZeroOut",
            "LoadImage", "ImageScale", "VAEEncode",
        ),
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae", "image_lora"),
        parameter_schema={
            "width": "integer", "height": "integer", "steps": "integer",
            "cfg": "number", "lora_strength": "number", "denoise": "number",
            "seed": "integer",
        },
        defaults={"steps": 8, "cfg": 1, "lora_strength": 1.0, "denoise": 0.65},
        limits={
            "references": 1, "steps": [8, 8], "cfg": [1, 1],
            "lora_strength": [0, 1.25], "denoise": [0.05, 1],
        },
        compiler="z_image_lora_img2img",
        license_id="Civitai Restricted License",
        license_url="https://civitai.com/models/2279079?modelVersionId=2565112",
        use_notice=(
            "BF16 主模型 + 完整 Qwen3-4B 文本编码器。"
            "实验性 latent img2img，非官方 Z-Image Edit，亦非官方模板。"
            "社区文件 ZITnsfwLoRA.safetensors，SHA256 "
            "44bf34ce695ebcec6ca17f7dc27511f8fc4204943114d6c7c41cd4559e75dbaf；"
            "Civitai 元数据 allowCommercialUse=RentCivit、allowDerivatives=false。"
            "本地按非商业处理，禁止二次分发和衍生训练；仅限合法、自愿成年人内容。"
            "denoise 推荐 0.35–0.80，技术边界 0.05–1。"
        ),
        model_bindings={
            "image_diffusion_model": "z_image_turbo_bf16.safetensors",
            "image_text_encoder": "qwen_3_4b.safetensors",
            "image_vae": "ae.safetensors",
            "image_lora": "ZITnsfwLoRA.safetensors",
        },
    ),
    _profile(
        id="qwen-image-2.1-bf16", version="1.0", display_name="Qwen-Image 2.1 BF16 · 文生图 / 1–10 图指令编辑",
        output_type="image", input_modalities=("text", "image"),
        required_nodes=COMPILER_BASELINES["qwen_image_21"]["nodes"],
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae"),
        parameter_schema=COMPILER_PARAMETERS["qwen_image_21"]["schema"],
        defaults=COMPILER_PARAMETERS["qwen_image_21"]["defaults"],
        limits=COMPILER_PARAMETERS["qwen_image_21"]["limits"],
        compiler="qwen_image_21",
        license_id="Qwen Research License",
        license_url="https://github.com/QwenLM/Qwen-Image-2.1/blob/main/LICENSE",
        use_notice="Qwen-Image 2.1 权重仅限研究与评估等非商业用途；商业使用须另行取得许可。使用 BF16 主模型、BF16 Qwen3-VL 8B 编码器和 BF16 VAE，不自动量化。",
        model_bindings={
            "image_diffusion_model": "qwen_image_2.1_bf16.safetensors",
            "image_text_encoder": "qwen3vl_8b_bf16.safetensors",
            "image_vae": "qwen_image_2.1_vae_bf16.safetensors",
        },
    ),
    _profile(
        id="qwen-image-2512-fp8-t2i", version="1.0", display_name="Qwen-Image 2512 FP8 · 高质量人像 / 细节 / 排版",
        output_type="image", input_modalities=("text",),
        required_nodes=FLOW_IMAGE_SHARED + ("CLIPTextEncode", "EmptySD3LatentImage"),
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae"),
        parameter_schema={"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        defaults={"steps": 50, "cfg": 4},
        limits={"references": 0, "steps": [20, 60], "cfg": [1, 10]},
        compiler="qwen_image_t2i",
        model_bindings={
            "image_diffusion_model": "qwen_image_2512_fp8_e4m3fn.safetensors",
            "image_text_encoder": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
            "image_vae": "qwen_image_vae.safetensors",
        },
    ),
    _profile(
        id="qwen-image-edit-2511-int8", version="1.0", display_name="Qwen-Image Edit 2511 INT8 · 高一致性图生图",
        output_type="image", input_modalities=("text", "image"),
        required_nodes=FLOW_IMAGE_SHARED + (
            "LoadImage", "ImageScale", "VAEEncode", "TextEncodeQwenImageEditPlus",
            "FluxKontextMultiReferenceLatentMethod", "CFGNorm",
        ),
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae"),
        parameter_schema={"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "denoise": "number", "seed": "integer"},
        defaults={"steps": 40, "cfg": 3, "denoise": 1},
        limits={"references": 1, "steps": [20, 60], "cfg": [1, 10], "denoise": [0.05, 1]},
        compiler="qwen_image_edit",
        model_bindings={
            "image_diffusion_model": "qwen_image_edit_2511_int8_convrot.safetensors",
            "image_text_encoder": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
            "image_vae": "qwen_image_vae.safetensors",
        },
    ),
    _profile(
        id="flux2-klein-4b-fp8", version="1.0",
        display_name="FLUX.2 Klein 4B FP8 · 4 步文生图 / 1–4 图参考",
        output_type="image", input_modalities=("text", "image"),
        required_nodes=FLUX2_KLEIN_SHARED,
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae"),
        parameter_schema={"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        defaults={"steps": 4, "cfg": 1},
        limits={"references": 4, "steps": [4, 4], "cfg": [1, 1]},
        compiler="flux2_klein",
        license_id="Apache-2.0",
        license_url="https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/blob/main/LICENSE.md",
        use_notice="4B 采用 Apache-2.0；仍须遵守适用法律与模型使用政策。",
        model_bindings={
            "image_diffusion_model": "flux-2-klein-4b-fp8.safetensors",
            "image_text_encoder": "qwen_3_4b.safetensors",
            "image_vae": "flux2-vae.safetensors",
        },
    ),
    _profile(
        id="flux2-klein-9b-fp8", version="1.0",
        display_name="FLUX.2 Klein 9B FP8 · 4 步高质量 / 1–4 图参考",
        output_type="image", input_modalities=("text", "image"),
        required_nodes=FLUX2_KLEIN_SHARED,
        required_models=("image_diffusion_model", "image_text_encoder", "image_vae"),
        parameter_schema={"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        defaults={"steps": 4, "cfg": 1},
        limits={"references": 4, "steps": [4, 4], "cfg": [1, 1]},
        compiler="flux2_klein",
        license_id="FLUX Non-Commercial License v2.0",
        license_url="https://huggingface.co/black-forest-labs/FLUX.2-klein-9B/blob/main/LICENSE.md",
        use_notice="9B 仅限非商业用途，并受模型使用政策约束。",
        model_bindings={
            "image_diffusion_model": "flux-2-klein-9b-fp8.safetensors",
            "image_text_encoder": "qwen_3_8b_fp8mixed.safetensors",
            "image_vae": "full_encoder_small_decoder.safetensors",
        },
    ),
    _profile(
        id="anything-v5-t2i", version="1.0", display_name="Anything V5 text-to-image",
        output_type="image", input_modalities=("text",), required_nodes=IMAGE_SHARED + ("EmptyLatentImage",),
        required_models=("image_checkpoint",),
        parameter_schema={"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "seed": "integer"},
        defaults={"steps": 24, "cfg": 7}, limits={"references": 0, "steps": [1, 100]}, compiler="checkpoint_t2i",
    ),
    _profile(
        id="anything-v5-img2img", version="1.0", display_name="Anything V5 image-to-image",
        output_type="image", input_modalities=("text", "image"),
        required_nodes=IMAGE_SHARED + ("LoadImage", "ImageScale", "VAEEncode"),
        required_models=("image_checkpoint",),
        parameter_schema={"width": "integer", "height": "integer", "steps": "integer", "cfg": "number", "denoise": "number", "seed": "integer"},
        defaults={"steps": 24, "cfg": 7, "denoise": 0.65}, limits={"references": 1, "steps": [1, 100], "denoise": [0.05, 1]}, compiler="checkpoint_img2img",
    ),
)


def _validate_manifest(value: Any, source: str) -> WorkflowProfile:
    if not isinstance(value, dict):
        raise ApiError(500, "profile_invalid", f"profile {source} must be a JSON object")
    required = {"id", "version", "display_name", "output_type", "input_modalities", "required_nodes", "required_models", "parameter_schema", "defaults", "limits", "compiler"}
    missing = required - value.keys()
    if missing:
        raise ApiError(500, "profile_invalid", f"profile {source} is missing: {', '.join(sorted(missing))}")
    compiler = value["compiler"]
    if not isinstance(compiler, str) or compiler not in ALLOWED_COMPILERS:
        raise ApiError(500, "profile_compiler", f"profile {source} uses an untrusted compiler")
    baseline = COMPILER_BASELINES[compiler]
    sampling_mode = value.get("sampling_mode", "turbo4" if compiler.startswith("h3_") else "default")
    allowed_sampling = {"turbo4", "base"} if compiler.startswith("h3_") else {"default"}
    if not isinstance(sampling_mode, str) or sampling_mode not in allowed_sampling:
        raise ApiError(500, "profile_invalid", f"profile {source} has an invalid sampling_mode")
    identifier = value["id"]
    if not isinstance(identifier, str) or not identifier or len(identifier) > 80 or not all(c.isalnum() or c in "-_." for c in identifier):
        raise ApiError(500, "profile_invalid", f"profile {source} has an invalid id")
    output = value["output_type"]
    modalities = value["input_modalities"]
    models = value["required_models"]
    nodes = value["required_nodes"]
    if not isinstance(output, str) or output not in {"video", "image"} or output != baseline["output_type"]:
        raise ApiError(500, "profile_invalid", f"profile {source} output does not match its compiler")
    if not isinstance(modalities, list) or any(not isinstance(item, str) for item in modalities) or not set(modalities) <= ALLOWED_MODALITIES:
        raise ApiError(500, "profile_invalid", f"profile {source} has invalid modalities")
    if not isinstance(models, list) or any(not isinstance(item, str) for item in models) or not set(models) <= ALLOWED_MODEL_ROLES:
        raise ApiError(500, "profile_invalid", f"profile {source} may only reference configured model roles")
    # Node names are declarative checks only. The compiler, never this list, builds the graph.
    if not isinstance(nodes, list) or any(not isinstance(node, str) or len(node) > 100 for node in nodes):
        raise ApiError(500, "profile_invalid", f"profile {source} has invalid required_nodes")
    objects = (value["parameter_schema"], value["defaults"], value["limits"])
    if any(not isinstance(item, dict) for item in objects):
        raise ApiError(500, "profile_invalid", f"profile {source} schemas must be objects")
    parameter_baseline = COMPILER_PARAMETERS[compiler]
    if sampling_mode == "base" and (
        "lora_strength" in value["parameter_schema"]
        or value["defaults"].get("lora_strength", 0) != 0
        or "LoraLoaderModelOnly" in nodes
        or any(role in {"fl_lora", "ref_lora"} for role in models)
    ):
        raise ApiError(500, "profile_invalid", f"profile {source} base sampling must not declare a Turbo LoRA")
    allowed_keys = set(parameter_baseline["schema"]) | {"references"}
    if any(not isinstance(key, str) for obj in objects for key in obj) or any(key not in allowed_keys for obj in objects for key in obj):
        raise ApiError(500, "profile_invalid", f"profile {source} declares unsupported parameters")
    schema = dict(parameter_baseline["schema"])
    for key, kind in value["parameter_schema"].items():
        if kind != schema.get(key):
            raise ApiError(500, "profile_invalid", f"profile {source} changes the trusted type of {key}")
    mode_defaults = {"steps": 4, "lora_strength": 0.75} if sampling_mode == "turbo4" else {}
    defaults = {**parameter_baseline["defaults"], **mode_defaults, **value["defaults"]}
    limits = dict(parameter_baseline["limits"])
    for key, proposed in value["limits"].items():
        baseline_limit = limits.get(key)
        if isinstance(baseline_limit, list):
            if not isinstance(proposed, list) or len(proposed) != 2 or any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in proposed):
                raise ApiError(500, "profile_invalid", f"profile {source} has invalid limits for {key}")
            narrowed = [max(float(baseline_limit[0]), float(proposed[0])), min(float(baseline_limit[1]), float(proposed[1]))]
            if narrowed[0] > narrowed[1]:
                raise ApiError(500, "profile_invalid", f"profile {source} has contradictory limits for {key}")
            limits[key] = narrowed
        elif isinstance(baseline_limit, int):
            if not isinstance(proposed, int) or isinstance(proposed, bool) or proposed < 0:
                raise ApiError(500, "profile_invalid", f"profile {source} has invalid limit for {key}")
            limits[key] = min(baseline_limit, proposed)
    for key, default in defaults.items():
        kind = schema.get(key)
        if kind in {"number", "integer"} and (not isinstance(default, (int, float)) or isinstance(default, bool)):
            raise ApiError(500, "profile_invalid", f"profile {source} has invalid default for {key}")
        if kind == "integer" and isinstance(default, float) and not default.is_integer():
            raise ApiError(500, "profile_invalid", f"profile {source} default for {key} must be an integer")
        if kind == "string" and not isinstance(default, str):
            raise ApiError(500, "profile_invalid", f"profile {source} has invalid default for {key}")
        bound = limits.get(key)
        if isinstance(bound, list) and isinstance(default, (int, float)) and not bound[0] <= default <= bound[1]:
            raise ApiError(500, "profile_invalid", f"profile {source} default for {key} is outside its limits")
    if sampling_mode == "base":
        defaults["lora_strength"] = 0
        schema.pop("lora_strength", None)
    bindings = value.get("model_bindings", {})
    if not isinstance(bindings, dict) or any(
        not isinstance(role, str) or role not in ALLOWED_MODEL_ROLES or not isinstance(name, str)
        for role, name in bindings.items()
    ):
        raise ApiError(500, "profile_invalid", f"profile {source} has invalid model_bindings")
    if sampling_mode == "base" and any(role in {"fl_lora", "ref_lora"} for role in bindings):
        raise ApiError(500, "profile_invalid", f"profile {source} base sampling must not bind a Turbo LoRA")
    for role, name in bindings.items():
        path = PurePosixPath(name)
        if not name or len(name) > 240 or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or "\\" in name or any(ord(char) < 32 for char in name):
            raise ApiError(500, "profile_invalid", f"profile {source} has an unsafe binding for {role}")
    resume = value.get("resume", {})
    if not isinstance(resume, dict):
        raise ApiError(500, "profile_invalid", f"profile {source} has an invalid resume declaration")
    if resume:
        supported = resume.get("supported") is True
        allowed_resume_keys = {"supported", "schedule_version", "max_total_steps", "additional_steps", "reason"}
        if set(resume) - allowed_resume_keys:
            raise ApiError(500, "profile_invalid", f"profile {source} has unsupported resume fields")
        if supported:
            maximum = resume.get("max_total_steps")
            additional = resume.get("additional_steps")
            step_limits = limits.get("steps")
            if (
                not compiler.startswith("h3_") or sampling_mode != "base"
                or resume.get("schedule_version") != "h3-simple-fixed/v1"
                or not isinstance(maximum, int) or isinstance(maximum, bool)
                or not isinstance(additional, list) or len(additional) != 2
                or any(not isinstance(item, int) or isinstance(item, bool) for item in additional)
                or not isinstance(step_limits, list) or maximum != int(step_limits[1])
                or additional[0] < 1 or additional[0] > additional[1]
                or additional[1] > maximum - int(step_limits[0])
            ):
                raise ApiError(500, "profile_invalid", f"profile {source} has an unsafe resume policy")
            nodes = list(dict.fromkeys((*nodes, *VIDEO_RESUME_NODES)))
    modalities = list(dict.fromkeys((*baseline["modalities"], *modalities)))
    nodes = list(dict.fromkeys((*baseline["nodes"], *nodes)))
    models = list(dict.fromkeys((*baseline["models"], *models)))
    if sampling_mode == "turbo4":
        nodes = list(dict.fromkeys((*nodes, "LoraLoaderModelOnly")))
        models = list(dict.fromkeys((*models, "ref_lora" if compiler == "h3_ref" else "fl_lora")))
    if any(role not in models for role in bindings):
        raise ApiError(500, "profile_invalid", f"profile {source} binds a model role it does not require")
    license_fields: dict[str, str] = {}
    for key in ("license_id", "license_url", "use_notice"):
        item = value.get(key, "")
        if not isinstance(item, str) or len(item) > 500 or any(ord(char) < 32 and char not in "\t\n" for char in item):
            raise ApiError(500, "profile_invalid", f"profile {source} has invalid {key}")
        license_fields[key] = item
    if license_fields["license_url"] and not license_fields["license_url"].startswith("https://"):
        raise ApiError(500, "profile_invalid", f"profile {source} license_url must use https")
    return WorkflowProfile(
        id=identifier, version=str(value["version"])[:32], display_name=str(value["display_name"])[:128],
        output_type=output, input_modalities=tuple(modalities), required_nodes=tuple(nodes),
        required_models=tuple(models), parameter_schema=schema, defaults=defaults,
        limits=limits, compiler=compiler, sampling_mode=sampling_mode,
        model_bindings=dict(bindings), resume=dict(resume), built_in=False, **license_fields,
    )


class ProfileRegistry:
    def __init__(self, profiles: Iterable[WorkflowProfile]) -> None:
        self._profiles: dict[str, WorkflowProfile] = {}
        for profile in profiles:
            if profile.id in self._profiles:
                raise ApiError(500, "profile_duplicate", f"duplicate profile id {profile.id}")
            self._profiles[profile.id] = profile

    @classmethod
    def load(cls, directory: Path | None = None) -> "ProfileRegistry":
        profiles = list(BUILTIN_PROFILES)
        if directory:
            directory.mkdir(parents=True, exist_ok=True)
            for path in sorted(directory.glob("*.json")):
                if path.stat().st_size > 256 * 1024:
                    raise ApiError(500, "profile_invalid", f"profile {path.name} is too large")
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise ApiError(500, "profile_invalid", f"cannot read profile {path.name}") from error
                profiles.append(_validate_manifest(raw, path.name))
        return cls(profiles)

    def get(self, profile_id: str) -> WorkflowProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as error:
            raise ApiError(400, "unknown_profile", f"unknown workflow profile {profile_id!r}") from error

    def all(self) -> tuple[WorkflowProfile, ...]:
        return tuple(self._profiles.values())

    def choose(self, output_type: str, mode: str, references: Iterable[Any], requested: str = "auto") -> WorkflowProfile:
        refs = tuple(references)
        if requested and requested != "auto":
            profile = self.get(requested)
            if profile.output_type != output_type:
                raise ApiError(400, "profile_mismatch", "profile output type does not match the request")
        else:
            if output_type == "image":
                identifier = "qwen-image-edit-2511-int8" if refs else "z-image-turbo-bf16-t2i"
            else:
                identifier = "minimax-h3-ref2va" if mode == "ref2va" else "minimax-h3-fl2va"
            profile = self.get(identifier)
        used_modalities = {"text", *(str(getattr(ref, "kind", "")) for ref in refs)}
        if not used_modalities <= set(profile.input_modalities):
            raise ApiError(400, "profile_mismatch", "profile does not accept the connected input modalities")
        maximum = profile.limits.get("references")
        if isinstance(maximum, int) and len(refs) > maximum:
            raise ApiError(400, "profile_mismatch", "profile reference limit was exceeded")
        return profile


DEFAULT_REGISTRY = ProfileRegistry(BUILTIN_PROFILES)


# Z-Image-Edit has not been published with a reviewed local graph/model binding.
# Keep roadmap information outside the executable registry: every registered
# profile must select a trusted compiler and must be honestly capability-probed.
UNAVAILABLE_IMAGE_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "id": "z-image-edit-unreleased",
        "version": None,
        "display_name": "Z-Image-Edit（尚未发布）",
        "output_type": "image",
        "input_modalities": ["text", "image"],
        "available": False,
        "selectable": False,
        "placeholder": True,
        "status": "unreleased",
        "reason": (
            "Z-Image-Edit 尚无已发布且经审核的本地模型绑定与官方工作流；"
            "实验性 latent img2img 不等同于 Z-Image-Edit。"
        ),
    },
)
