"""ACE-Step's local Python API under H3's existing exclusive GPU lease."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class AceStepEngine:
    def __init__(self, repo: Path, models: Path, device: int):
        os.environ.update(CUDA_VISIBLE_DEVICES=str(device), ACESTEP_CHECKPOINTS_DIR=str(models),
                          HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        sys.path.insert(0, str(repo))
        with contextlib.redirect_stdout(sys.stderr):
            from acestep import model_downloader
            # Upstream's default precheck insists on Turbo + LM even for XL Cover.
            # Check only the three components this adapter actually loads.
            model_downloader.MAIN_MODEL_COMPONENTS = ["acestep-v15-xl-sft", "vae", "Qwen3-Embedding-0.6B"]
            from acestep.handler import AceStepHandler
            from acestep.llm_inference import LLMHandler
            self.handler = AceStepHandler()
            status, ok = self.handler.initialize_service(
                project_root=str(repo), config_path="acestep-v15-xl-sft", device="cuda",
                use_flash_attention=False, compile_model=False, quantization=None,
                offload_to_cpu=False, offload_dit_to_cpu=False, vae_checkpoint="official")
            if not ok:
                raise RuntimeError(status)
            import torch
            if self.handler.dtype != torch.bfloat16 or self.handler.quantization is not None:
                raise RuntimeError("XL-SFT requires BF16 without quantization")
            self.lm = LLMHandler()  # Cover uses source audio; no LM weights are loaded.

    def run(self, request: dict) -> Path:
        with contextlib.redirect_stdout(sys.stderr):
            return self._run(request)

    def _run(self, request):
        from acestep.inference import GenerationParams, GenerationConfig, generate_music
        import numpy as np
        import soundfile as sf
        output = Path(request["output"])
        with tempfile.TemporaryDirectory(prefix="acestep-", dir=output.parent) as temp:
            source = Path(temp) / "source.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", request["source"],
                            "-ac", "2", "-ar", "48000", "-c:a", "pcm_f32le", str(source)], check=True, timeout=300)
            duration = sf.info(source).duration
            if not 10 <= duration <= 180:
                raise ValueError("ACE-Step 当前支持 10–180 秒歌曲，请先裁剪片段。")
            p = request["parameters"]
            params = GenerationParams(task_type="cover", src_audio=str(source),
                reference_audio=request["reference"] if request["reference"] != request["source"] else None,
                lyrics=request["lyrics"], caption=p.get("caption", ""), vocal_language="zh",
                duration=duration, inference_steps=p["diffusion_steps"], guidance_scale=p["inference_cfg_rate"],
                seed=p["seed"], audio_cover_strength=p.get("audio_cover_strength", 1.0),
                thinking=False, use_cot_metas=False, use_cot_caption=False, use_cot_language=False)
            result = generate_music(self.handler, self.lm, params,
                GenerationConfig(batch_size=1, use_random_seed=False, seeds=[p["seed"]], audio_format="wav"), save_dir=temp)
            if not result.success or not result.audios:
                raise RuntimeError(result.error or result.status_message or "ACE-Step returned no audio")
            generated = Path(result.audios[0]["path"]).resolve()
            if not generated.is_relative_to(Path(temp).resolve()):
                raise RuntimeError("ACE-Step returned an unexpected output path")
            audio, rate = sf.read(generated, dtype="float32", always_2d=True)
            if not np.isfinite(audio).all() or not audio.size or np.max(np.abs(audio)) < 1e-5:
                raise RuntimeError("ACE-Step produced empty or non-finite audio")
            # Cover output must stay on the source timeline for A/B playback.
            target = round(duration * rate)
            if abs(len(audio) - target) > rate:
                raise RuntimeError("ACE-Step output duration differs from source by more than one second")
            audio = np.pad(audio[:target], ((0, max(0, target-len(audio))), (0, 0)))
            sf.write(output, audio, rate, subtype="PCM_16")
            (output.parent / "generation.json").write_text(json.dumps({"model": "acestep-v15-xl-sft", "precision": "bf16", "task_type": "cover", "duration": duration, "parameters": p}, ensure_ascii=False))
        return output
