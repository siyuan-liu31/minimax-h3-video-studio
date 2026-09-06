"""Minimal, timeout-bounded ComfyUI HTTP client."""

from __future__ import annotations

import json
import hashlib
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import Config
from .errors import ApiError, CapabilityError
from .profiles import (
    DEFAULT_REGISTRY, H3_MAX_DURATION_SECONDS, H3_MAX_REFERENCES, UNAVAILABLE_IMAGE_CAPABILITIES,
    ProfileRegistry, WorkflowProfile,
)
from .workflows import GenerationSpec


class ComfyClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._cache_lock = threading.Lock()
        self._object_info_cache: tuple[float, dict[str, Any]] | None = None
        self._resource_lock = threading.RLock()
        self._resident_h3_key: tuple[tuple[str, str], ...] | None = None
        self._known_clean = False
        self._last_busy_at = time.monotonic()
        self._idle_free_done = False
        self._idle_monitor_stop = threading.Event()
        self._idle_monitor_thread: threading.Thread | None = None

    def request(
        self,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        if not path.startswith("/"):
            raise ValueError("ComfyUI request path must start with /")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return {} if not raw.strip() else json.loads(raw)
        except urllib.error.HTTPError as error:
            detail = error.read(16 * 1024).decode("utf-8", errors="replace")
            raise ApiError(
                502,
                "comfy_rejected",
                "ComfyUI rejected the request",
                details={"status": error.code, "response": detail},
            ) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ApiError(502, "comfy_unreachable", f"cannot reach ComfyUI: {error}") from error

    def health(self) -> dict[str, Any]:
        value = self.request("/system_stats", timeout=5)
        return value if isinstance(value, dict) else {}

    def execution_environment(self, config: Config) -> dict[str, str]:
        """Return auditable accelerator/attention facts for safety policies."""

        architecture = config.gpu_architecture
        if architecture.lower() == "auto":
            stats = self.health()
            candidates: list[str] = []

            def visit(value: Any, key: str = "") -> None:
                if isinstance(value, dict):
                    for child_key, child in value.items():
                        visit(child, str(child_key))
                elif isinstance(value, list):
                    for child in value:
                        visit(child, key)
                elif any(marker in key.lower() for marker in ("architecture", "compute_capability", "cuda_capability", "device_name")):
                    candidates.append(str(value))
                elif key.lower() == "name" and isinstance(value, str) and any(
                    marker in value.lower() for marker in ("rtx 50", "rtx pro 6000 blackwell", "blackwell")
                ):
                    # Current ComfyUI system_stats exposes an RTX 5090 only as
                    # devices[].name, without CUDA compute capability.
                    candidates.append(value)

            visit(stats)
            architecture = " ".join(candidates) if candidates else "unknown"
            if re.search(r"\bRTX\s+50\d{2}\b", architecture, re.IGNORECASE) or "blackwell" in architecture.lower():
                architecture = f"sm120 Blackwell ({architecture})"
        return {
            "gpu_architecture": architecture,
            "attention_backend": config.attention_backend,
        }

    @staticmethod
    def _queue_busy(queue: dict[str, Any]) -> bool:
        return bool(queue.get("queue_running") or queue.get("queue_pending"))

    @staticmethod
    def _h3_resource_key(workflow: dict[str, Any]) -> tuple[tuple[str, str], ...] | None:
        """Return one canonical key for the H3 loader resource set.

        ComfyUI caches loader node outputs.  Keeping the loader identities and
        their scalar inputs stable lets identical H3 prompts reuse the same
        UNet, text encoder, VAEs, and optional LoRA instead of establishing a
        second resident resource set.
        """

        nodes = [node for node in workflow.values() if isinstance(node, dict)]
        if not any(str(node.get("class_type", "")).startswith("MiniMaxH3") for node in nodes):
            return None
        loader_types = {"UNETLoader", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly"}
        resources: list[tuple[str, str]] = []
        for node in nodes:
            class_type = str(node.get("class_type", ""))
            if class_type not in loader_types:
                continue
            inputs = node.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            scalar_inputs = {
                str(name): value
                for name, value in inputs.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }
            resources.append((class_type, json.dumps(scalar_inputs, sort_keys=True, separators=(",", ":"))))
        return tuple(sorted(resources))

    def workflow_resource_key(self, workflow: dict[str, Any]) -> str:
        """Stable resident key for every reviewed ComfyUI loader graph."""

        h3 = self._h3_resource_key(workflow)
        if h3 is not None:
            raw = json.dumps(h3, sort_keys=True, separators=(",", ":")).encode()
            return "h3:" + hashlib.sha256(raw).hexdigest()
        loader_types = {
            "CheckpointLoaderSimple", "UNETLoader", "CLIPLoader", "VAELoader",
            "LoraLoader", "LoraLoaderModelOnly", "DualCLIPLoader",
        }
        resources: list[tuple[str, str]] = []
        for node in workflow.values():
            if not isinstance(node, dict) or str(node.get("class_type", "")) not in loader_types:
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            scalar = {str(key): value for key, value in inputs.items() if isinstance(value, (str, int, float, bool)) or value is None}
            resources.append((str(node["class_type"]), json.dumps(scalar, sort_keys=True, separators=(",", ":"))))
        raw = json.dumps(sorted(resources), separators=(",", ":")).encode()
        return "comfy:" + hashlib.sha256(raw).hexdigest()

    def _free_memory_locked(self) -> None:
        self.request(
            "/free",
            payload={"unload_models": True, "free_memory": True},
            timeout=30,
        )
        self._resident_h3_key = None
        self._known_clean = True
        self._idle_free_done = True

    def free_memory(self) -> None:
        """Unload ComfyUI models and clear its execution cache."""

        with self._resource_lock:
            queue = self.queue()
            if self._queue_busy(queue):
                raise ApiError(409, "comfy_busy", "ComfyUI has queued or running work; memory was not released")
            self._free_memory_locked()

    def free_memory_if_idle(self, idle_seconds: int, *, now: float | None = None) -> bool:
        """Release ComfyUI once after a globally idle interval."""

        if idle_seconds <= 0:
            return False
        current = time.monotonic() if now is None else now
        with self._resource_lock:
            queue = self.queue()
            if self._queue_busy(queue):
                self._last_busy_at = current
                self._idle_free_done = False
                return False
            if self._idle_free_done or current - self._last_busy_at < idle_seconds:
                return False
            self._free_memory_locked()
            return True

    def start_idle_free_monitor(self, idle_seconds: int, poll_seconds: int) -> None:
        if idle_seconds <= 0 or self._idle_monitor_thread is not None:
            return
        self._idle_monitor_stop.clear()

        def monitor() -> None:
            while not self._idle_monitor_stop.wait(max(1, poll_seconds)):
                try:
                    self.free_memory_if_idle(idle_seconds)
                except ApiError:
                    # A transient ComfyUI outage must not terminate the API or
                    # disable future cleanup attempts.
                    continue

        self._idle_monitor_thread = threading.Thread(
            target=monitor,
            name="h3-studio-comfy-idle-free",
            daemon=True,
        )
        self._idle_monitor_thread.start()

    def stop_idle_free_monitor(self) -> None:
        thread = self._idle_monitor_thread
        if thread is None:
            return
        self._idle_monitor_stop.set()
        thread.join(timeout=2)
        self._idle_monitor_thread = None

    def object_info(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._cache_lock:
            if not force and self._object_info_cache and now - self._object_info_cache[0] < 30:
                return self._object_info_cache[1]
        value = self.request("/object_info", timeout=60)
        if not isinstance(value, dict):
            raise ApiError(502, "comfy_response", "ComfyUI returned invalid node metadata")
        with self._cache_lock:
            self._object_info_cache = (now, value)
        return value

    @staticmethod
    def _choices(info: dict[str, Any], node: str, input_name: str) -> set[str]:
        try:
            definition = info[node]["input"]["required"][input_name]
            first = definition[0]
        except (KeyError, IndexError, TypeError):
            return set()
        if isinstance(first, list):
            return {str(x) for x in first}
        if isinstance(first, tuple):
            return {str(x) for x in first}
        # ComfyUI's newer schema represents combo inputs as
        # ["COMBO", {"options": [...]}], while older releases expose the
        # choices directly as the first list item.  Capability probing must
        # understand both or a valid workflow is incorrectly disabled.
        if first == "COMBO" and len(definition) > 1 and isinstance(definition[1], dict):
            options = definition[1].get("options")
            if isinstance(options, (list, tuple)):
                return {str(x) for x in options}
        return set()

    @staticmethod
    def _input_names(info: dict[str, Any], node: str) -> set[str]:
        try:
            inputs = info[node]["input"]
        except (KeyError, TypeError):
            return set()
        if not isinstance(inputs, dict):
            return set()
        names: set[str] = set()
        for group in ("required", "optional"):
            values = inputs.get(group, {})
            if isinstance(values, dict):
                names.update(str(name) for name in values)
        return names

    def _profile_capability(self, profile: WorkflowProfile, info: dict[str, Any], config: Config) -> dict[str, Any]:
        model_choices = {
            "fl_model": self._choices(info, "UNETLoader", "unet_name"),
            "ref_model": self._choices(info, "UNETLoader", "unet_name"),
            "text_encoder": self._choices(info, "CLIPLoader", "clip_name"),
            "video_vae": self._choices(info, "VAELoader", "vae_name"),
            "audio_vae": self._choices(info, "VAELoader", "vae_name"),
            "fl_lora": self._choices(info, "LoraLoaderModelOnly", "lora_name"),
            "ref_lora": self._choices(info, "LoraLoaderModelOnly", "lora_name"),
            "image_checkpoint": self._choices(info, "CheckpointLoaderSimple", "ckpt_name"),
            "image_diffusion_model": self._choices(info, "UNETLoader", "unet_name"),
            "image_text_encoder": self._choices(info, "CLIPLoader", "clip_name"),
            "image_vae": self._choices(info, "VAELoader", "vae_name"),
            "image_lora": self._choices(info, "LoraLoaderModelOnly", "lora_name"),
        }
        missing_nodes = sorted(set(profile.required_nodes) - info.keys())
        missing_models = []
        missing_model_files = []
        for role in profile.required_models:
            configured = profile.model_bindings.get(role, str(getattr(config, role, "")))
            if not configured or configured not in model_choices.get(role, set()):
                missing_models.append(role)
                if configured:
                    missing_model_files.append(configured)
        missing_options: list[str] = []
        if profile.compiler in {"h3_fl", "h3_ref"}:
            sampler = "sa_solver" if profile.sampling_mode == "turbo4" else "res_multistep"
            scheduler = "simple"
            if sampler not in self._choices(info, "KSamplerSelect", "sampler_name"):
                missing_options.append(f"KSamplerSelect.sampler_name={sampler}")
            if scheduler not in self._choices(info, "BasicScheduler", "scheduler"):
                missing_options.append(f"BasicScheduler.scheduler={scheduler}")
        elif profile.compiler in {
            "z_image_t2i", "z_image_img2img", "z_image_lora_t2i", "z_image_lora_img2img",
            "qwen_image_t2i", "qwen_image_edit",
        }:
            sampler = "res_multistep" if profile.compiler in {
                "z_image_t2i", "z_image_img2img", "z_image_lora_t2i", "z_image_lora_img2img",
            } else "euler"
            scheduler = "simple"
            if sampler not in self._choices(info, "KSampler", "sampler_name"):
                missing_options.append(f"KSampler.sampler_name={sampler}")
            if scheduler not in self._choices(info, "KSampler", "scheduler"):
                missing_options.append(f"KSampler.scheduler={scheduler}")
            if profile.compiler in {
                "z_image_img2img", "z_image_lora_t2i", "z_image_lora_img2img",
            }:
                if "lumina2" not in self._choices(info, "CLIPLoader", "type"):
                    missing_options.append("CLIPLoader.type=lumina2")
                required_inputs = {
                    "ModelSamplingAuraFlow": {"model", "shift"},
                    "KSampler": {
                        "model", "positive", "negative", "latent_image", "seed",
                        "steps", "cfg", "sampler_name", "scheduler", "denoise",
                    },
                }
                if profile.compiler in {"z_image_lora_t2i", "z_image_lora_img2img"}:
                    required_inputs["LoraLoaderModelOnly"] = {
                        "model", "lora_name", "strength_model",
                    }
                if profile.compiler in {"z_image_img2img", "z_image_lora_img2img"}:
                    required_inputs.update({
                        "ImageScale": {"image", "upscale_method", "width", "height", "crop"},
                        "VAEEncode": {"pixels", "vae"},
                    })
                for node, expected_inputs in required_inputs.items():
                    if node in info:
                        missing_inputs = sorted(expected_inputs - self._input_names(info, node))
                        if missing_inputs:
                            missing_options.append(f"{node}.inputs={','.join(missing_inputs)}")
        elif profile.compiler == "flux2_klein":
            if "euler" not in self._choices(info, "KSamplerSelect", "sampler_name"):
                missing_options.append("KSamplerSelect.sampler_name=euler")
            if "flux2" not in self._choices(info, "CLIPLoader", "type"):
                missing_options.append("CLIPLoader.type=flux2")
            required_inputs = {
                "ReferenceLatent": {"conditioning", "latent"},
                "ImageScaleToTotalPixels": {"image", "upscale_method", "megapixels", "resolution_steps"},
                "EmptyFlux2LatentImage": {"width", "height", "batch_size"},
                "Flux2Scheduler": {"steps", "width", "height"},
                "CFGGuider": {"model", "positive", "negative", "cfg"},
                "SamplerCustomAdvanced": {"noise", "guider", "sampler", "sigmas", "latent_image"},
            }
            for node, expected_inputs in required_inputs.items():
                if node in info:
                    missing_inputs = sorted(expected_inputs - self._input_names(info, node))
                    if missing_inputs:
                        missing_options.append(f"{node}.inputs={','.join(missing_inputs)}")
        result = profile.public()
        result.update({
            "available": not missing_nodes and not missing_models and not missing_options,
            "missing_nodes": missing_nodes,
            "missing_models": missing_models,
            "missing_model_files": missing_model_files,
            "missing_options": missing_options,
        })
        return result

    def capabilities(self, config: Config, registry: ProfileRegistry = DEFAULT_REGISTRY) -> dict[str, Any]:
        info = self.object_info()
        profile_values = [self._profile_capability(profile, info, config) for profile in registry.all()]
        video_nodes = {
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "LoraLoaderModelOnly",
            "PathchSageAttentionKJ",
            "MiniMaxH3MemoryEfficientSageAttentionPatch",
            "MiniMaxH3ImageToVideo",
            "MiniMaxH3ReferenceToVideo",
            "RandomNoise",
            "BasicGuider",
            "KSamplerSelect",
            "BasicScheduler",
            "SamplerCustomAdvanced",
            "VAEDecode",
            "VAEDecodeAudio",
            "CreateVideo",
            "SaveVideo",
        }
        missing_video_nodes = sorted(video_nodes - info.keys())
        unets = self._choices(info, "UNETLoader", "unet_name")
        loras = self._choices(info, "LoraLoaderModelOnly", "lora_name")
        clips = self._choices(info, "CLIPLoader", "clip_name")
        vaes = self._choices(info, "VAELoader", "vae_name")
        checkpoints = self._choices(info, "CheckpointLoaderSimple", "ckpt_name")
        image_nodes = {
            "CheckpointLoaderSimple",
            "CLIPTextEncode",
            "EmptyLatentImage",
            "KSampler",
            "VAEDecode",
            "SaveImage",
        }
        missing_image_nodes = sorted(image_nodes - info.keys())
        missing_shared_files = [
            name
            for name, choices in (
                (config.text_encoder, clips),
                (config.video_vae, vaes),
                (config.audio_vae, vaes),
            )
            if name not in choices
        ]
        fl_available = (
            not missing_video_nodes
            and not missing_shared_files
            and config.fl_model in unets
            and config.fl_lora in loras
        )
        ref_available = (
            not missing_video_nodes
            and not missing_shared_files
            and config.ref_model in unets
            and config.ref_lora in loras
            and all(name in info for name in ("LoadImage", "LoadVideo", "LoadAudio", "GetVideoComponents"))
        )
        fl_available = any(p["available"] for p in profile_values if p["compiler"] == "h3_fl")
        ref_available = any(p["available"] for p in profile_values if p["compiler"] == "h3_ref")
        image_profiles = [profile for profile in profile_values if profile["output_type"] == "image"]
        motion_required_inputs = {
            "MiniMaxH3MotionContext": {
                "conditioning", "vae", "latent", "context_length",
                "audio_context_length", "context_latent",
            },
            "MiniMaxH3MotionContextTrim": {
                "images", "trim_frames", "audio", "fps", "match_tail",
            },
            "H3StudioSaveLatent": {"samples", "video_done", "filename_prefix"},
            "H3StudioLoadLatent": {"latent"},
        }
        motion_missing_nodes = sorted(set(motion_required_inputs) - info.keys())
        motion_missing_inputs = sorted(
            f"{node}.inputs={','.join(missing)}"
            for node, expected in motion_required_inputs.items()
            if node in info
            for missing in [sorted(expected - self._input_names(info, node))]
            if missing
        )
        motion_context = {
            "available": not motion_missing_nodes and not motion_missing_inputs,
            "missing_nodes": motion_missing_nodes,
            "missing_inputs": motion_missing_inputs,
            "video_frames": [5, 22, 39, 56],
            "audio_frames": {"min": 0, "max": 240, "default": 24},
            "defaults": {"video_frames": 22, "audio_frames": 24, "match_tail": True},
            "sampling_modes": ["turbo4", "base"],
            "upstream": {
                "repository": "https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context",
                "tag": "v0.5.1",
                "commit": "429e952ae5c09b54f44cb6e3bef7331d998f0656",
                "license": "GPL-3.0",
            },
        }
        return {
            "profiles": profile_values,
            "video": {
                "available": fl_available or ref_available,
                "modes": {
                    "text": fl_available,
                    "fl2va": fl_available and "LoadImage" in info,
                    "ref2va": ref_available,
                },
                "missing_nodes": missing_video_nodes,
                "missing_shared_files": missing_shared_files,
                "configured_models": {
                    "fl2va": config.fl_model,
                    "ref2va": config.ref_model,
                    "fl_lora": config.fl_lora,
                    "ref_lora": config.ref_lora,
                },
                "configured_loras_present": {
                    "fl2va": config.fl_lora in loras,
                    "ref2va": config.ref_lora in loras,
                },
                "resolutions": {
                    "16:9": [1344, 768],
                    "9:16": [768, 1344],
                    "1:1": [1024, 1024],
                },
                "duration_seconds": {"min": 5, "max": H3_MAX_DURATION_SECONDS},
                "max_references": H3_MAX_REFERENCES,
                "motion_context": motion_context,
            },
            "image": {
                "available": any(profile["available"] for profile in image_profiles),
                "checkpoint": config.image_checkpoint or None,
                "installed_checkpoints": sorted(checkpoints),
                "missing_nodes": missing_image_nodes,
                "modes": {
                    "text-to-image": any(
                        p["available"] and "image" not in p["input_modalities"] for p in image_profiles
                    ),
                    "image-to-image": any(
                        p["available"] and "image" in p["input_modalities"] for p in image_profiles
                    ),
                },
                "unavailable_profiles": [dict(value) for value in UNAVAILABLE_IMAGE_CAPABILITIES],
            },
        }

    def ensure_capability(self, spec: GenerationSpec, config: Config, registry: ProfileRegistry = DEFAULT_REGISTRY) -> None:
        capabilities = self.capabilities(config, registry)
        selected = next((profile for profile in capabilities["profiles"] if profile["id"] == spec.profile_id), None)
        if not selected:
            raise CapabilityError("selected workflow profile is not registered")
        if not selected["available"]:
            missing = ", ".join(
                selected.get("missing_models", [])
                + selected.get("missing_model_files", [])
                + selected.get("missing_nodes", [])
                + selected.get("missing_options", [])
            )
            raise CapabilityError(f"selected workflow profile is unavailable; missing {missing}", details=selected)
        if spec.output_type == "image":
            return

    def ensure_motion_context(self, config: Config, registry: ProfileRegistry = DEFAULT_REGISTRY) -> None:
        capability = self.capabilities(config, registry)["video"]["motion_context"]
        if capability.get("available") is True:
            return
        missing = ", ".join(
            list(capability.get("missing_nodes", []))
            + list(capability.get("missing_inputs", []))
        )
        raise CapabilityError(
            f"Motion Context is unavailable; missing {missing}",
            details=capability,
        )

    def submit(self, workflow: dict[str, Any], client_id: str) -> str:
        resource_key = self._h3_resource_key(workflow)
        with self._resource_lock:
            if resource_key is not None and (
                not self._known_clean or resource_key != self._resident_h3_key
            ):
                queue = self.queue()
                if self._queue_busy(queue):
                    raise ApiError(
                        409,
                        "h3_model_switch_busy",
                        "another ComfyUI resource set is active; retry this H3 model switch after the queue is empty",
                    )
                # Establish a known-clean cache on the first H3 request after
                # an API restart, and evict the previous H3 resource set before
                # switching model/encoder/VAE/LoRA bindings.
                if not self._known_clean or self._resident_h3_key is not None:
                    self._free_memory_locked()
            value = self.request("/prompt", payload={"prompt": workflow, "client_id": client_id}, timeout=60)
            prompt_id = value.get("prompt_id") if isinstance(value, dict) else None
            if not isinstance(prompt_id, str) or not prompt_id:
                raise ApiError(502, "comfy_response", "ComfyUI did not return a prompt id")
            if resource_key is not None:
                self._resident_h3_key = resource_key
                self._known_clean = True
            else:
                # A non-H3 workflow may load another heavy resource set behind
                # our back.  The next H3 switch must re-establish a clean cache.
                self._known_clean = False
            self._last_busy_at = time.monotonic()
            self._idle_free_done = False
            return prompt_id

    def cancel(self, prompt_id: str) -> None:
        queue = self.queue()
        running_items = [item for item in queue.get("queue_running", []) if isinstance(item, list) and len(item) > 1]
        running = any(item[1] == prompt_id for item in running_items)
        pending = any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in queue.get("queue_pending", []))
        if pending:
            self.request("/queue", payload={"delete": [prompt_id]})
        elif running and len(running_items) == 1:
            # ComfyUI's interrupt endpoint is process-wide. Only interrupt when
            # the requested prompt is the task currently running.
            self.request("/interrupt", payload={})
        elif running:
            raise ApiError(409, "cancel_ambiguous", "ComfyUI reports multiple running prompts; refusing a process-wide interrupt")

    def find_prompt_by_client_id(self, client_id: str) -> str | None:
        """Reconcile the submit/metadata crash window from queue *and* history.

        A completed prompt disappears from ``/queue``.  Current ComfyUI keeps
        the caller's client id in ``record.prompt[3].client_id`` in global
        history, so searching only the queue can strand a paid completion in
        ``submitting`` forever.
        """
        queue = self.queue()
        for key in ("queue_running", "queue_pending"):
            for item in queue.get(key, []):
                if not isinstance(item, list) or len(item) < 4 or not isinstance(item[1], str):
                    continue
                extra = item[3]
                if isinstance(extra, dict) and extra.get("client_id") == client_id:
                    return item[1]
        value = self.request("/history?max_items=200")
        if isinstance(value, dict):
            for key, record in value.items():
                if not isinstance(record, dict):
                    continue
                prompt = record.get("prompt")
                if not isinstance(prompt, list) or len(prompt) < 4:
                    continue
                extra = prompt[3]
                if isinstance(extra, dict) and extra.get("client_id") == client_id:
                    candidate = prompt[1] if len(prompt) > 1 else key
                    if isinstance(candidate, str) and candidate:
                        return candidate
        return None

    def history(self, prompt_id: str) -> dict[str, Any] | None:
        encoded = urllib.parse.quote(prompt_id, safe="")
        value = self.request(f"/history/{encoded}")
        if not isinstance(value, dict):
            return None
        record = value.get(prompt_id)
        return record if isinstance(record, dict) else None

    def queue(self) -> dict[str, Any]:
        value = self.request("/queue")
        return value if isinstance(value, dict) else {}

    def status(self, prompt_id: str) -> dict[str, Any]:
        record = self.history(prompt_id)
        if record is not None:
            status = record.get("status", {})
            completed = isinstance(status, dict) and status.get("completed") is True
            state = "completed" if completed else str(
                status.get("status_str", "error") if isinstance(status, dict) else "error"
            )
            result: dict[str, Any] = {"status": state, "record": record}
            if state == "error" and isinstance(status, dict):
                result["message"] = "ComfyUI generation failed"
                messages = status.get("messages")
                if isinstance(messages, list):
                    result["details"] = messages[-3:]
            return result
        queue = self.queue()
        for item in queue.get("queue_running", []):
            if isinstance(item, list) and len(item) > 1 and item[1] == prompt_id:
                return {"status": "running"}
        for item in queue.get("queue_pending", []):
            if isinstance(item, list) and len(item) > 1 and item[1] == prompt_id:
                return {"status": "queued"}
        return {"status": "not_found"}


def find_outputs(record: dict[str, Any], output_type: str) -> list[dict[str, str]]:
    extensions = (
        {".mp4", ".mov", ".webm", ".mkv"}
        if output_type == "video"
        else {".png", ".jpg", ".jpeg", ".webp"}
    )
    found: list[dict[str, str]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            filename = value.get("filename")
            if isinstance(filename, str) and any(filename.lower().endswith(ext) for ext in extensions):
                found.append(
                    {
                        "filename": filename,
                        "subfolder": str(value.get("subfolder", "")),
                        "type": str(value.get("type", "output")),
                    }
                )
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(record.get("outputs", {}))
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for item in found:
        key = (item["filename"], item["subfolder"], item["type"])
        unique[key] = item
    return list(unique.values())
