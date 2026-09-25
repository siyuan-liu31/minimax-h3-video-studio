"""Validated contract for the pinned SoulX / ComfyUI-MIDI-Edit adapter."""
from __future__ import annotations

import math
import secrets
from pathlib import Path
from typing import Any
from .errors import ApiError

MODEL_REVISION = "40493ad90286056c7a9095035164434a79daa8c9"
PREPROCESS_REVISION = "83dc50289d22a81b1e9998f5b9e111aef7c1fdcd"
REQUIRED_MODELS = (
    "SoulX-Singer/model.pt",
    "SoulX-Singer/config.yaml",
    "SoulX-Singer-Preprocess/mel-band-roformer-karaoke/mel_band_roformer_karaoke_becruily.ckpt",
    "SoulX-Singer-Preprocess/mel-band-roformer-karaoke/config_karaoke_becruily.yaml",
    "SoulX-Singer-Preprocess/dereverb_mel_band_roformer/dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt",
    "SoulX-Singer-Preprocess/dereverb_mel_band_roformer/dereverb_mel_band_roformer_anvuew.yaml",
    "SoulX-Singer-Preprocess/rmvpe/rmvpe.pt",
    "SoulX-Singer-Preprocess/rosvot/rosvot/model.pt",
    "SoulX-Singer-Preprocess/rosvot/rosvot/config.yaml",
    "SoulX-Singer-Preprocess/rosvot/rwbd/model.pt",
    "SoulX-Singer-Preprocess/rosvot/rwbd/config.yaml",
    "SoulX-Singer-Preprocess/rosvot/rmvpe/model.pt",
    "SoulX-Singer-Preprocess/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch/model.pt",
)


def rewrite_parameters(data: dict[str, Any]):
    lyrics = {}
    for key in ("lyrics", "original_lyrics"):
        value = data.get(key, "")
        if not isinstance(value, str) or len(value) > 10000 or "\x00" in value or (key == "lyrics" and not value.strip()):
            raise ApiError(400, "invalid_lyrics", f"{key} must be text of at most 10000 characters; new lyrics cannot be empty")
        lyrics[key] = value
    steps, cfg, seed = data.get("diffusion_steps", 32), data.get("inference_cfg_rate", 3), data.get("seed", -1)
    if type(steps) is not int or not 16 <= steps <= 100:
        raise ApiError(400, "invalid_parameter", "SoulX diffusion_steps must be 16..100")
    if type(cfg) not in (int, float) or not math.isfinite(cfg) or not 0 <= cfg <= 10:
        raise ApiError(400, "invalid_parameter", "SoulX inference_cfg_rate must be 0..10")
    if type(seed) is not int or not -1 <= seed <= 2**32 - 1:
        raise ApiError(400, "invalid_parameter", "seed must be -1 or 0..4294967295")
    requested = {"diffusion_steps": steps, "inference_cfg_rate": float(cfg), "seed": seed}
    return lyrics, requested, {**requested, "seed": secrets.randbelow(2**32) if seed == -1 else seed}


def capability(config) -> dict[str, Any]:
    root = Path(config.soulx_root) if config.soulx_root else None
    base = Path(config.soulx_models) / "Soul-AILab" if config.soulx_models else None
    missing = []
    if root is None or not (root / "core/soulsx_singer.py").is_file() or not (root / "SoulX-Singer/cli/inference.py").is_file():
        missing.append("SoulX runtime")
    if not config.soulx_python or not Path(config.soulx_python).is_file():
        missing.append("python")
    if base is None:
        missing.append("models")
    else:
        missing.extend(name for name in REQUIRED_MODELS if not (base / name).is_file())
    return {"id": "soulx", "available": not missing, "mode": "lyrics_rewrite",
            "root": config.soulx_root, "python": config.soulx_python,
            "repository_revision": config.soulx_revision, "model_revision": MODEL_REVISION,
            "preprocess_revision": PREPROCESS_REVISION, "missing": missing,
            "reason": "; ".join(missing) if missing else None}
