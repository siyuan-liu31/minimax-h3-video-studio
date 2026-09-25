"""Pinned ACE-Step XL-SFT cover contract; no implicit smaller model fallback."""
from pathlib import Path
import math
from .errors import ApiError
from .soulx import rewrite_parameters as base_parameters

REPOSITORY_REVISION = "ca1e85fe9430179831e6bc6be790c332190a3866"
MODEL_REVISION = "d06de46b4622f781cf07f4a013a67d591ca52819"
COMPONENT_REVISION = "19671f406d603126926c1b7e2adc169acbcade22"
MODEL = "acestep-v15-xl-sft"
REQUIRED = [f"{MODEL}/model-{i:05d}-of-00004.safetensors" for i in range(1, 5)] + [
    f"{MODEL}/configuration_acestep_v15.py", f"{MODEL}/modeling_acestep_v15_xl_base.py", f"{MODEL}/apg_guidance.py",
    f"{MODEL}/config.json", f"{MODEL}/model.safetensors.index.json", f"{MODEL}/silence_latent.pt",
    "vae/config.json", "vae/diffusion_pytorch_model.safetensors",
    "Qwen3-Embedding-0.6B/model.safetensors", "Qwen3-Embedding-0.6B/tokenizer.json",
    "Qwen3-Embedding-0.6B/config.json",
]


def rewrite_parameters(data):
    lyrics, requested, effective = base_parameters({"diffusion_steps": 50, "inference_cfg_rate": 7, **data})
    if len(lyrics["lyrics"]) > 4096:
        raise ApiError(400, "invalid_lyrics", "ACE-Step lyrics must be at most 4096 characters")
    caption = data.get("caption", "")
    strength = data.get("audio_cover_strength", 1.0)
    if not isinstance(caption, str) or len(caption) > 512 or "\x00" in caption:
        raise ApiError(400, "invalid_parameter", "caption must be text of at most 512 characters")
    if type(strength) not in (int, float) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ApiError(400, "invalid_parameter", "audio_cover_strength must be 0..1")
    for value in (requested, effective):
        value.update(caption=caption, audio_cover_strength=float(strength))
    return lyrics, requested, effective


def capability(config):
    root, models = Path(config.acestep_root), Path(config.acestep_models)
    missing = []
    if not config.acestep_root or not (root / "acestep/inference.py").is_file():
        missing.append("ACE-Step runtime")
    if not config.acestep_python or not Path(config.acestep_python).is_file():
        missing.append("python")
    missing.extend(name for name in REQUIRED if not config.acestep_models or not (models / name).is_file())
    return {"id": "acestep", "available": not missing, "mode": "lyrics_cover", "model": MODEL,
            "precision": "bf16", "quantization": None, "repository_revision": REPOSITORY_REVISION,
            "model_revision": MODEL_REVISION, "component_revision": COMPONENT_REVISION,
            "root": config.acestep_root, "python": config.acestep_python, "missing": missing,
            "reason": "; ".join(missing) if missing else None,
            "lyrics": {"max_length": 4096, "transcribe": False, "preview": False, "preserve_melody": False},
            "tuning": {"diffusion_steps": {"default": 50, "minimum": 16, "maximum": 100},
                       "inference_cfg_rate": {"default": 7, "minimum": 0, "maximum": 10},
                       "seed": {"default": -1, "minimum": -1, "maximum": 2**32-1}},
            "cover": {"audio_cover_strength": {"default": 1, "minimum": 0, "maximum": 1},
                      "caption_max_length": 512, "duration_seconds": [10, 180]}}
