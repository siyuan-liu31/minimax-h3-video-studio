"""Persistent inference subprocess for reviewed Vevo2 and YingMusic-SVC roots.

Stdout is reserved for one-line JSON RPC responses.  Upstream libraries are
redirected to stderr because several print progress during imports and model
loading.  The API process terminates this worker on cancellation or backend
switch, which releases the complete CUDA process context.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import traceback
import types
from pathlib import Path
from typing import Any


YING_AUXILIARY_REVISIONS = {
    "lj1995/VoiceConversionWebUI": "e6d0c1a17da07c33557852f9dfa2bd44cc75737d",
    "funasr/campplus": "e4b6ede7ce16997aff4ae69fbca1f0175e2afede",
    "nvidia/bigvgan_v2_44khz_128band_512x": "95a9d1dcb12906c03edd938d77b9333d6ded7dfb",
    "openai/whisper-small": "973afd24965f72e36ca33b3055d56a652f456b4d",
}


def _prepend_sys_path(path: Path) -> None:
    resolved = str(path.resolve())
    while resolved in sys.path:
        sys.path.remove(resolved)
    sys.path.insert(0, resolved)


def _ensure_torchaudio_wav_io() -> None:
    """Keep upstream WAV input/output working without TorchCodec.

    Current torchaudio delegates every save to TorchCodec, while the pinned
    Vevo2 code only needs to write a floating-point tensor as WAV.  SoundFile
    provides that exact operation and is already required by both runtimes.
    """
    try:
        import torchcodec  # noqa: F401
        return
    except ImportError:
        pass

    import soundfile as sf
    import torch
    import torchaudio

    def save(uri, src, sample_rate, *args, **kwargs):
        del args, kwargs
        if not isinstance(src, torch.Tensor) or src.ndim not in (1, 2):
            raise ValueError("torchaudio WAV compatibility save expects a one- or two-dimensional tensor")
        samples = src.detach().to("cpu", dtype=torch.float32)
        if samples.ndim == 1:
            samples = samples.unsqueeze(0)
        sf.write(str(uri), samples.numpy().T, int(sample_rate), format="WAV", subtype="FLOAT")

    def load(uri, frame_offset=0, num_frames=-1, normalize=True, channels_first=True, *args, **kwargs):
        del normalize, args, kwargs
        frames = -1 if int(num_frames) < 0 else int(num_frames)
        samples, sample_rate = sf.read(
            str(uri), start=int(frame_offset), frames=frames,
            dtype="float32", always_2d=True,
        )
        tensor = torch.from_numpy(samples.copy())
        if channels_first:
            tensor = tensor.T
        return tensor, int(sample_rate)

    torchaudio.save = save
    torchaudio.load = load


def _ensure_torchaudio_sox_effects() -> None:
    """Restore the small torchaudio SoX API used by YingMusic's remixer.

    Torchaudio removed ``sox_effects`` in current CUDA 13 builds required by
    Blackwell GPUs.  YingMusic only calls ``apply_effects_tensor`` for its
    reviewed echo/reverb chain, so bridge that exact API to the system SoX
    binary without patching the pinned upstream checkout or changing effect
    parameters.
    """
    import soundfile as sf
    import torch
    import torchaudio

    if getattr(torchaudio, "sox_effects", None) is not None:
        return
    if shutil.which("sox") is None:
        raise RuntimeError("YingMusic remix requires the system sox executable")

    def apply_effects_tensor(waveform, sample_rate, effects, *args, **kwargs):
        del args, kwargs
        with tempfile.TemporaryDirectory(prefix="yingmusic-sox-") as temporary:
            source = Path(temporary) / "source.wav"
            output = Path(temporary) / "output.wav"
            samples = waveform.detach().to("cpu", dtype=torch.float32).numpy()
            sf.write(source, samples.T, int(sample_rate), subtype="FLOAT")
            command = ["sox", str(source), str(output)]
            for effect in effects:
                if not isinstance(effect, (list, tuple)) or not effect:
                    raise ValueError("invalid SoX effect specification")
                command.extend(str(value) for value in effect)
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
            if completed.returncode or not output.is_file():
                detail = completed.stderr.strip()[-1000:]
                raise RuntimeError(f"SoX effect failed: {detail or 'no output'}")
            rendered, rendered_rate = sf.read(output, dtype="float32", always_2d=True)
            tensor = torch.from_numpy(rendered.T.copy()).to(dtype=waveform.dtype)
            return tensor, int(rendered_rate)

    torchaudio.sox_effects = types.SimpleNamespace(apply_effects_tensor=apply_effects_tensor)


class Vevo2Engine:
    def __init__(self, repo: Path, cache_root: Path, device_index: int, model_revision: str) -> None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(device_index)
        os.environ["HF_HOME"] = str(cache_root / "huggingface")
        os.chdir(repo)
        _prepend_sys_path(repo)
        with contextlib.redirect_stdout(sys.stderr):
            _ensure_torchaudio_wav_io()
            from models.svc.vevo2 import infer_vevo2_fm as module

            self.module = module
            original_download = module.snapshot_download

            def pinned_download(*args, **kwargs):
                kwargs["revision"] = model_revision
                kwargs["allow_patterns"] = [
                    "tokenizer/contentstyle_fvq16384_12.5hz/*",
                    "acoustic_modeling/fm_emilia101k_singnet7k_repa/*",
                    "vocoder/*",
                ]
                return original_download(*args, **kwargs)

            module.snapshot_download = pinned_download
            self.pipeline = module.load_inference_pipeline()
            module.inference_pipeline = self.pipeline

    def run(self, request: dict[str, Any]) -> Path:
        output = Path(str(request["output"])).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stdout(sys.stderr):
            self.module.vevo2_fm(
                str(Path(str(request["source"])).resolve()),
                str(Path(str(request["reference"])).resolve()),
                str(output),
                shifted_src=True,
            )
        return output


class YingMusicEngine:
    def __init__(
        self,
        repo: Path,
        cache_root: Path,
        device_index: int,
        *,
        separator_config: Path,
        separator_checkpoint: Path,
        svc_config: Path,
        svc_checkpoint: Path,
    ) -> None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(device_index)
        os.environ["HF_HOME"] = str(cache_root / "huggingface")
        os.environ["HF_HUB_CACHE"] = str(cache_root / "huggingface" / "hub")
        os.chdir(repo)
        _prepend_sys_path(repo)
        # The pinned separator imports ``models`` and ``utils`` as top-level
        # packages, matching its standalone inference.py entry point.
        _prepend_sys_path(repo / "accom_separation")
        self.repo = repo
        self.cache_root = cache_root
        self.device_index = device_index
        self.separator_config = separator_config
        self.separator_checkpoint = separator_checkpoint
        self.svc_config = svc_config
        self.svc_checkpoint = svc_checkpoint
        with contextlib.redirect_stdout(sys.stderr):
            self._load_models()

    def _load_models(self) -> None:
        _ensure_torchaudio_wav_io()
        _ensure_torchaudio_sox_effects()
        import torch
        import yaml
        from huggingface_hub import hf_hub_download
        from accom_separation.utils.model_utils import load_start_checkpoint
        from accom_separation.utils.settings import get_model_from_config, parse_args_inference
        import my_inference as inference_module

        # YingMusic's module assigns a relative cache during import.  Restore
        # the server-owned persistent cache before any model is downloaded.
        os.environ["HF_HOME"] = str(self.cache_root / "huggingface")
        os.environ["HF_HUB_CACHE"] = str(self.cache_root / "huggingface" / "hub")
        hub_cache = self.cache_root / "huggingface" / "hub"
        hub_cache.mkdir(parents=True, exist_ok=True)

        def pinned_file(repo_id, filename="pytorch_model.bin", config_filename=None):
            revision = YING_AUXILIARY_REVISIONS.get(repo_id)
            if revision is None:
                raise RuntimeError(f"unreviewed YingMusic auxiliary model: {repo_id}")
            model_path = hf_hub_download(
                repo_id=repo_id, filename=filename, revision=revision,
                cache_dir=str(hub_cache),
            )
            if config_filename is None:
                return model_path
            config_path = hf_hub_download(
                repo_id=repo_id, filename=config_filename, revision=revision,
                cache_dir=str(hub_cache),
            )
            return model_path, config_path

        # Pin the two direct hf_utils downloads instead of allowing mutable
        # repository heads to enter a long-lived production worker.
        inference_module.load_custom_model_from_hf = pinned_file
        bigvgan_config = Path(pinned_file(
            "nvidia/bigvgan_v2_44khz_128band_512x", "config.json",
        ))
        pinned_file("nvidia/bigvgan_v2_44khz_128band_512x", "bigvgan_generator.pt")
        whisper_config = Path(pinned_file("openai/whisper-small", "config.json"))
        pinned_file("openai/whisper-small", "model.safetensors")
        pinned_file("openai/whisper-small", "preprocessor_config.json")

        # Upstream reads model identifiers from YAML.  Preserve every model
        # parameter while replacing just those identifiers with the exact
        # cached snapshots prepared above.
        runtime_config = self.cache_root / "yingmusic-runtime-config.yaml"
        config_value = yaml.safe_load(self.svc_config.read_text(encoding="utf-8"))
        config_value["model_params"]["vocoder"]["name"] = str(bigvgan_config.parent)
        config_value["model_params"]["speech_tokenizer"]["name"] = str(whisper_config.parent)
        runtime_config.write_text(yaml.safe_dump(config_value, sort_keys=False), encoding="utf-8")

        self.torch = torch
        self.parse_args_inference = parse_args_inference
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        args = parse_args_inference({
            "model_type": "bs_roformer",
            "config_path": str(self.separator_config),
            "start_check_point": str(self.separator_checkpoint),
            "input_folder": ".",
            "store_dir": ".",
            "extract_instrumental": True,
            "extract_other": False,
            "device_ids": [0],
            "disable_detailed_pbar": True,
            "force_cpu": False,
            "flac_file": False,
            "use_tta": False,
        })
        separator, separator_config = get_model_from_config(args.model_type, args.config_path)
        checkpoint = torch.load(args.start_check_point, weights_only=False, map_location="cpu")
        load_start_checkpoint(args, separator, checkpoint, type_="inference")
        self.separator = separator.eval().to(self.device)
        self.separator_runtime_config = separator_config

        svc_args = types.SimpleNamespace(
            source="", target="", diffusion_steps=100,
            checkpoint=str(self.svc_checkpoint), expname="worker",
            cuda=self.device, fp16=True, accompany=None,
            config=str(runtime_config), length_adjust=1.0,
            inference_cfg_rate=0.7, f0_condition=True,
            semi_tone_shift=None, output=".", uuid="worker",
        )
        self.svc_args = svc_args
        self.svc_bundle = inference_module.load_models_api(svc_args, device=self.device)
        self.run_inference = inference_module.run_inference

    @staticmethod
    def _locate(directory: Path, names: tuple[str, ...]) -> Path | None:
        for name in names:
            for suffix in (".wav", ".flac"):
                candidate = directory / f"{name}{suffix}"
                if candidate.is_file():
                    return candidate
        return None

    def run(self, request: dict[str, Any]) -> Path:
        from accom_separation.inference import run_folder
        from Remix.auger import echo_then_reverb_save
        import numpy as np

        source = Path(str(request["source"])).resolve()
        reference = Path(str(request["reference"])).resolve()
        output = Path(str(request["output"])).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        # All stems and intermediate conversions are task-scoped and removed
        # on success, failure, cancellation, or worker termination.
        with tempfile.TemporaryDirectory(prefix="yingmusic-", dir=str(output.parent)) as temporary:
            work_root = Path(temporary)
            input_dir = work_root / "input"
            stem_dir = work_root / "stems"
            input_dir.mkdir()
            copied = input_dir / source.name
            shutil.copy2(source, copied)
            args = self.parse_args_inference({
                "model_type": "bs_roformer",
                "config_path": str(self.separator_config),
                "start_check_point": str(self.separator_checkpoint),
                "input_folder": str(input_dir),
                "store_dir": str(stem_dir),
                "extract_instrumental": True,
                "extract_other": False,
                "device_ids": [0],
                "disable_detailed_pbar": True,
                "force_cpu": False,
                "flac_file": False,
                "use_tta": False,
            })
            with contextlib.redirect_stdout(sys.stderr):
                run_folder(self.separator, args, self.separator_runtime_config, self.device, verbose=True)
            song_dir = stem_dir / copied.stem
            lead = self._locate(song_dir, ("vocals", "lead", "leading_vocals"))
            accompany = self._locate(song_dir, ("instrumental", "accompaniment"))
            if lead is None or accompany is None:
                raise RuntimeError("YingMusic separator did not produce both lead vocal and accompaniment")

            self.svc_args.source = str(lead)
            self.svc_args.target = str(reference)
            self.svc_args.accompany = str(accompany)
            self.svc_args.output = str(work_root / "svc")
            self.svc_args.expname = "converted"
            self.svc_args.uuid = str(request.get("task_id", "worker"))
            parameters = request.get("parameters") or {}
            self.svc_args.diffusion_steps = int(parameters.get("diffusion_steps", 100))
            self.svc_args.inference_cfg_rate = float(parameters.get("inference_cfg_rate", 0.7))
            seed = parameters.get("seed")
            if seed is not None:
                seed = int(seed)
                random.seed(seed)
                np.random.seed(seed)
                self.torch.manual_seed(seed)
                if self.torch.cuda.is_available():
                    self.torch.cuda.manual_seed_all(seed)
            with contextlib.redirect_stdout(sys.stderr):
                converted = Path(self.run_inference(self.svc_args, self.svc_bundle, device=self.device))
                mixed = work_root / "mixed.wav"
                echo_then_reverb_save(str(converted), str(mixed), str(accompany))
            shutil.copy2(mixed, output)
        return output


def _engine(args: argparse.Namespace):
    repo = Path(args.repo).resolve()
    cache = Path(args.cache_root).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    if args.engine == "vevo2":
        return Vevo2Engine(repo, cache, args.device, args.model_revision)
    return YingMusicEngine(
        repo, cache, args.device,
        separator_config=Path(args.separator_config).resolve(),
        separator_checkpoint=Path(args.separator_checkpoint).resolve(),
        svc_config=Path(args.svc_config).resolve(),
        svc_checkpoint=Path(args.svc_checkpoint).resolve(),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=("vevo2", "yingmusic"), required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--model-revision", default="")
    parser.add_argument("--separator-config", default="")
    parser.add_argument("--separator-checkpoint", default="")
    parser.add_argument("--svc-config", default="")
    parser.add_argument("--svc-checkpoint", default="")
    args = parser.parse_args()
    try:
        engine = _engine(args)
    except Exception as error:
        print(json.dumps({"ready": False, "error": str(error)}), flush=True)
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({"ready": True, "engine": args.engine}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            if request.get("action") == "shutdown":
                print(json.dumps({"ok": True}), flush=True)
                return
            if request.get("action") != "run":
                raise ValueError("unsupported worker action")
            output = engine.run(request)
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("voice worker produced no output")
            print(json.dumps({"ok": True, "output": str(output), "bytes": output.stat().st_size}), flush=True)
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            print(json.dumps({"ok": False, "error": str(error)}), flush=True)


if __name__ == "__main__":
    main()
