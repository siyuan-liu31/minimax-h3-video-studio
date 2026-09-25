"""Environment-backed, side-effect-free server configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _integer(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _bounded_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = _integer(name, default, minimum)
    if value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    host: str
    port: int
    api_key: str
    cors_origins: tuple[str, ...]
    comfy_url: str
    data_root: Path
    comfy_input: Path
    comfy_output: Path
    max_json_bytes: int
    max_image_bytes: int
    max_video_bytes: int
    max_audio_bytes: int
    max_asset_storage_bytes: int
    max_active_jobs: int
    asset_ttl_days: int
    fl_model: str
    ref_model: str
    text_encoder: str
    video_vae: str
    audio_vae: str
    fl_lora: str
    ref_lora: str
    image_checkpoint: str
    # Recovery and long-project limits intentionally have defaults so older
    # programmatic Config constructors remain source compatible.
    submit_reconcile_grace_seconds: int = 120
    # A 1,000-segment project may legitimately contain several kilobytes of
    # structured prompt text per segment.  Keep the ordinary API JSON limit
    # small, but give this authenticated project endpoint a bounded 32 MiB
    # envelope so the advertised segment boundary is practically reachable.
    max_project_json_bytes: int = 32 * 1024 * 1024
    max_merged_output_bytes: int = 200 * 1024 * 1024 * 1024
    merge_timeout_min_seconds: int = 300
    merge_timeout_factor: int = 2
    # ComfyUI owns the actual model objects.  The API process only coordinates
    # their lifetime through the global queue and /free endpoint.
    comfy_idle_free_seconds: int = 180
    comfy_idle_poll_seconds: int = 15
    checkpoint_ttl_hours: int = 48
    checkpoint_gc_seconds: int = 30 * 60
    # Motion Context latents are durable project state, not Python/GPU memory.
    # Superseded chain links are pruned and the remaining files share this
    # explicit disk budget.
    max_motion_context_storage_bytes: int = 200 * 1024 * 1024 * 1024
    gpu_architecture: str = "auto"
    attention_backend: str = "SageAttention"
    h3_token_risk_threshold: int = 150_000
    # One physical GPU is shared by ComfyUI and external voice workers.  The
    # scheduler keeps original model precision/settings and resolves pressure
    # through exclusive leases and safe model release instead.
    gpu_device_index: int = 0
    gpu_idle_release_seconds: int = 180
    gpu_poll_seconds: int = 2
    max_active_voice_tasks: int = 16
    voice_worker_start_seconds: int = 20 * 60
    acestep_root: str = ""
    acestep_python: str = ""
    acestep_models: str = ""
    soulx_root: str = ""
    soulx_python: str = ""
    soulx_models: str = ""
    soulx_revision: str = "801cb858464dd33312dcec27ed7151d130ae93ee"
    vevo2_root: str = ""
    vevo2_python: str = ""
    vevo2_revision: str = "26f6883110181f1dbfe95c70a7c7dbaf4de5f42a"
    vevo2_model_revision: str = "2674843cbaa50aa89ee7ccaf5bb15d6ccf46c6c8"
    yingmusic_root: str = ""
    yingmusic_python: str = ""
    yingmusic_revision: str = "4974a80c6044c4557059548409379f6365129f88"
    yingmusic_model_revision: str = "da6b73938afeb7ede4c8d93ef007af2abb04ef49"
    yingmusic_separator_config: str = ""
    yingmusic_separator_checkpoint: str = ""
    yingmusic_svc_config: str = ""
    yingmusic_svc_checkpoint: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        workspace = Path.cwd().resolve()
        raw_origins = os.environ.get(
            "H3_STUDIO_CORS_ORIGINS",
            "http://127.0.0.1:3013,http://localhost:3013",
        )
        origins = tuple(x.strip() for x in raw_origins.split(",") if x.strip())
        if not origins:
            origins = ("http://127.0.0.1:3013", "http://localhost:3013")
        api_key = os.environ.get("H3_STUDIO_API_KEY", "")
        if "*" in origins and not api_key:
            raise ValueError(
                "H3_STUDIO_API_KEY must be non-empty when H3_STUDIO_CORS_ORIGINS contains *"
            )
        return cls(
            host=os.environ.get("H3_STUDIO_HOST", "127.0.0.1"),
            port=_integer("H3_STUDIO_PORT", 6020),
            api_key=api_key,
            cors_origins=origins,
            comfy_url=os.environ.get("COMFY_URL", "http://127.0.0.1:6006").rstrip("/"),
            data_root=Path(
                os.environ.get("H3_STUDIO_DATA_ROOT", str(workspace / "data"))
            ).resolve(),
            comfy_input=Path(
                os.environ.get(
                    "H3_STUDIO_COMFY_INPUT", str(workspace / "comfy-input")
                )
            ).resolve(),
            comfy_output=Path(
                os.environ.get(
                    "H3_STUDIO_COMFY_OUTPUT", str(workspace / "comfy-output")
                )
            ).resolve(),
            max_json_bytes=_integer("H3_STUDIO_MAX_JSON_BYTES", 256 * 1024),
            max_image_bytes=_integer("H3_STUDIO_MAX_IMAGE_BYTES", 40 * 1024 * 1024),
            max_video_bytes=_integer("H3_STUDIO_MAX_VIDEO_BYTES", 1024 * 1024 * 1024),
            max_audio_bytes=_integer("H3_STUDIO_MAX_AUDIO_BYTES", 256 * 1024 * 1024),
            max_asset_storage_bytes=_integer("H3_STUDIO_MAX_ASSET_STORAGE_BYTES", 200 * 1024 * 1024 * 1024),
            max_active_jobs=_integer("H3_STUDIO_MAX_ACTIVE_JOBS", 4),
            asset_ttl_days=_integer("H3_STUDIO_ASSET_TTL_DAYS", 30),
            fl_model=os.environ.get(
                "H3_STUDIO_FL_MODEL",
                "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            ),
            ref_model=os.environ.get(
                "H3_STUDIO_REF_MODEL",
                "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
            ),
            text_encoder=os.environ.get(
                "H3_STUDIO_TEXT_ENCODER",
                "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            ),
            video_vae=os.environ.get(
                "H3_STUDIO_VIDEO_VAE", "minimax_h3_video_vae_fp16.safetensors"
            ),
            audio_vae=os.environ.get(
                "H3_STUDIO_AUDIO_VAE", "minimax_h3_audio_vae_fp32.safetensors"
            ),
            fl_lora=os.environ.get(
                "H3_STUDIO_FL_LORA",
                "minimax_h3_fl2v_lightx2v_turbo_4step_v1.0_768p_"
                "resized_avg_rank_31_bf16.safetensors",
            ),
            ref_lora=os.environ.get(
                "H3_STUDIO_REF_LORA",
                "minimax_h3_ref2v_lightx2v_turbo_4step_v0.1_"
                "resized_avg_rank_20_bf16.safetensors",
            ),
            image_checkpoint=os.environ.get(
                "H3_STUDIO_IMAGE_CHECKPOINT", "anything-v5-PrtRE.safetensors"
            ).strip(),
            submit_reconcile_grace_seconds=_integer(
                "H3_STUDIO_SUBMIT_RECONCILE_GRACE_SECONDS", 120
            ),
            max_project_json_bytes=_integer(
                "H3_STUDIO_MAX_PROJECT_JSON_BYTES", 32 * 1024 * 1024
            ),
            max_merged_output_bytes=_integer(
                "H3_STUDIO_MAX_MERGED_OUTPUT_BYTES", 200 * 1024 * 1024 * 1024
            ),
            merge_timeout_min_seconds=_integer(
                "H3_STUDIO_MERGE_TIMEOUT_MIN_SECONDS", 300
            ),
            merge_timeout_factor=_integer(
                "H3_STUDIO_MERGE_TIMEOUT_FACTOR", 2
            ),
            comfy_idle_free_seconds=_integer(
                "H3_STUDIO_COMFY_IDLE_FREE_SECONDS", 180, minimum=0
            ),
            comfy_idle_poll_seconds=_integer(
                "H3_STUDIO_COMFY_IDLE_POLL_SECONDS", 15
            ),
            checkpoint_ttl_hours=_bounded_integer(
                "H3_STUDIO_CHECKPOINT_TTL_HOURS", 48, 24, 72
            ),
            checkpoint_gc_seconds=_integer(
                "H3_STUDIO_CHECKPOINT_GC_SECONDS", 30 * 60, minimum=0
            ),
            max_motion_context_storage_bytes=_integer(
                "H3_STUDIO_MAX_MOTION_CONTEXT_STORAGE_BYTES",
                200 * 1024 * 1024 * 1024,
            ),
            gpu_architecture=os.environ.get("H3_STUDIO_GPU_ARCHITECTURE", "auto").strip() or "auto",
            attention_backend=os.environ.get("H3_STUDIO_ATTENTION_BACKEND", "SageAttention").strip() or "SageAttention",
            h3_token_risk_threshold=_integer("H3_STUDIO_H3_TOKEN_RISK_THRESHOLD", 150_000),
            gpu_device_index=_integer("H3_STUDIO_GPU_DEVICE_INDEX", 0, minimum=0),
            gpu_idle_release_seconds=_integer(
                "H3_STUDIO_GPU_IDLE_RELEASE_SECONDS",
                _integer("H3_STUDIO_COMFY_IDLE_FREE_SECONDS", 180, minimum=0),
                minimum=0,
            ),
            gpu_poll_seconds=_integer("H3_STUDIO_GPU_POLL_SECONDS", 2),
            max_active_voice_tasks=_integer("H3_STUDIO_MAX_ACTIVE_VOICE_TASKS", 16),
            voice_worker_start_seconds=_integer("H3_STUDIO_VOICE_WORKER_START_SECONDS", 20 * 60),
            vevo2_root=os.environ.get("H3_STUDIO_VEVO2_ROOT", "").strip(),
            vevo2_python=os.environ.get("H3_STUDIO_VEVO2_PYTHON", "").strip(),
            vevo2_revision=os.environ.get(
                "H3_STUDIO_VEVO2_REVISION", "26f6883110181f1dbfe95c70a7c7dbaf4de5f42a",
            ).strip(),
            vevo2_model_revision=os.environ.get(
                "H3_STUDIO_VEVO2_MODEL_REVISION", "2674843cbaa50aa89ee7ccaf5bb15d6ccf46c6c8",
            ).strip(),
            acestep_root=os.environ.get("H3_STUDIO_ACESTEP_ROOT", "").strip(),
            acestep_python=os.environ.get("H3_STUDIO_ACESTEP_PYTHON", "").strip(),
            acestep_models=os.environ.get("H3_STUDIO_ACESTEP_MODELS", "").strip(),
            soulx_root=os.environ.get("H3_STUDIO_SOULX_ROOT", "").strip(),
            soulx_python=os.environ.get("H3_STUDIO_SOULX_PYTHON", "").strip(),
            soulx_models=os.environ.get("H3_STUDIO_SOULX_MODELS", "").strip(),
            yingmusic_root=os.environ.get("H3_STUDIO_YINGMUSIC_ROOT", "").strip(),
            yingmusic_python=os.environ.get("H3_STUDIO_YINGMUSIC_PYTHON", "").strip(),
            yingmusic_revision=os.environ.get(
                "H3_STUDIO_YINGMUSIC_REVISION", "4974a80c6044c4557059548409379f6365129f88",
            ).strip(),
            yingmusic_model_revision=os.environ.get(
                "H3_STUDIO_YINGMUSIC_MODEL_REVISION", "da6b73938afeb7ede4c8d93ef007af2abb04ef49",
            ).strip(),
            yingmusic_separator_config=os.environ.get("H3_STUDIO_YINGMUSIC_SEPARATOR_CONFIG", "").strip(),
            yingmusic_separator_checkpoint=os.environ.get("H3_STUDIO_YINGMUSIC_SEPARATOR_CHECKPOINT", "").strip(),
            yingmusic_svc_config=os.environ.get("H3_STUDIO_YINGMUSIC_SVC_CONFIG", "").strip(),
            yingmusic_svc_checkpoint=os.environ.get("H3_STUDIO_YINGMUSIC_SVC_CHECKPOINT", "").strip(),
        )

    def prepare(self) -> None:
        for path in (
            self.data_root,
            self.data_root / "metadata" / "assets",
            self.data_root / "metadata" / "jobs",
            self.data_root / "metadata" / "asset-folders",
            self.data_root / "metadata" / "derivations",
            self.data_root / "metadata" / "checkpoints",
            self.data_root / "metadata" / "motion-context",
            self.data_root / "metadata" / "voice-tasks",
            self.data_root / "derivations",
            self.data_root / "checkpoints",
            self.data_root / "motion-context",
            self.data_root / "voice-results",
            self.data_root / "model-cache",
            self.data_root / "logs",
            self.data_root / "thumbnails",
            self.data_root / "tmp",
            self.comfy_input / "h3-studio",
            self.comfy_input / "h3-studio-motion-context",
        ):
            path.mkdir(parents=True, exist_ok=True)
