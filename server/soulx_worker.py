"""Adapter for pinned ComfyUI-MIDI-Edit core; no upstream sources are modified.

Keep raw source segment timestamps: upstream's public MIDI conversion drops
those offsets, which would move vocals relative to the original accompaniment.
"""
from __future__ import annotations

import contextlib
import json
import os
import gc
import subprocess
import sys
import tempfile
from pathlib import Path


class SoulXEngine:
    def __init__(self, repo: Path, models: Path, device: int):
        os.environ["CUDA_VISIBLE_DEVICES"] = str(device)
        sys.path.insert(0, str(repo))
        sys.path.insert(0, str(repo / "SoulX-Singer"))
        with contextlib.redirect_stdout(sys.stderr):
            from core import soulsx_singer
            # Upstream hides import errors as None; fail with the real cause.
            from preprocess.tools.vocal_separation.model import VocalSeparator
            from preprocess.tools.note_transcription.model import NoteTranscriber
            from preprocess.tools.lyric_transcription import LyricTranscriber
            self.core = soulsx_singer
            self.core.set_models_base(str(models))
        self.models = models

    def run(self, request: dict) -> Path:
        with contextlib.redirect_stdout(sys.stderr):
            return self._run(request)

    def _run(self, request: dict) -> Path:
        import numpy as np
        import soundfile as sf
        import torch
        from voice_worker import _ensure_torchaudio_wav_io
        _ensure_torchaudio_wav_io()
        output = Path(request["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="soulx-", dir=output.parent) as temporary:
            work = Path(temporary)
            # Upstream preprocessing writes adjacent JSON; only pass task-owned copies.
            source = work / "source.wav"
            reference = work / "reference.wav"
            for original, target in ((request["source"], source), (request["reference"], reference)):
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", original, "-c:a", "pcm_f32le", str(target)], check=True, timeout=300)
            source_info = sf.info(source)
            # Capture the separator output without another lossy separation pass.
            original_factory = self.core._get_preprocess_pipeline
            captured = {}
            def factory(*args, **kwargs):
                pipeline = original_factory(*args, **kwargs)
                process = pipeline.vocal_separator.process
                def separate(path):
                    result = process(path)
                    captured["vocal"] = result.vocals_dereverbed.T.copy()
                    # Subtract the full original vocal, including its reverb.
                    captured["accompaniment"] = (result.mix - result.vocals).T.copy()
                    captured["sample_rate"] = result.sample_rate
                    return result
                pipeline.vocal_separator.process = separate
                return pipeline
            self.core._get_preprocess_pipeline = factory
            try:
                source_meta, detected = self.core._preprocess_audio_to_metadata(
                    str(source), language="Mandarin", max_merge_duration=30000,
                    reference_lyrics=request.get("original_lyrics") or None)
                source_stems = dict(captured)
                if request["source"] == request["reference"]:
                    prompt_meta = source_meta
                    prompt_vocal = source_stems["vocal"]
                    prompt_rate = source_stems["sample_rate"]
                else:
                    prompt_meta, _ = self.core._preprocess_audio_to_metadata(str(reference), language="Mandarin", max_merge_duration=30000)
                    prompt_vocal, prompt_rate = captured["vocal"], captured["sample_rate"]
            finally:
                self.core._get_preprocess_pipeline = original_factory
            # DataProcessor reads from sample zero, regardless of metadata time.
            # Crop to the first prompt segment before pairing waveform and tokens.
            prompt_start, prompt_end = prompt_meta[0]["time"]
            prompt_vocal = prompt_vocal[round(prompt_start * prompt_rate / 1000):round(prompt_end * prompt_rate / 1000)]
            sf.write(reference, prompt_vocal, prompt_rate, subtype="FLOAT")
            midi = self.core.metadata_to_midi_json(source_meta)
            from dataclasses import asdict
            from core.midi_format import parse_tracks, serialize_track, Track, Token
            from core.align_algorithm import _build_units
            from core.g2p import normalize_digits
            from soulx_alignment import align_tracks
            lines = [_build_units(normalize_digits(line)) for line in request["lyrics"].splitlines() if line.strip()]
            aligned = align_tracks([asdict(t) for t in parse_tracks(midi)], [line for line in lines if line])
            tracks = []
            for item in aligned:
                track = serialize_track(Track(tokens=[Token(**t) for t in item["tokens"]], meta=item["meta"], f0=item["f0"]))
                # Upstream rounds durations to centiseconds; avoid accumulated drift.
                track["duration"] = " ".join(f"{t['duration']:.8f}" for t in item["tokens"])
                tracks.append(track)
            warnings = []
            if len(tracks) != len(source_meta):
                raise RuntimeError("lyric alignment changed segment count")
            (output.parent / "alignment.json").write_text(json.dumps({"original": source_meta, "edited": tracks, "detected_lyrics": detected, "warnings": warnings}, ensure_ascii=False), encoding="utf-8")
            gc.collect()
            torch.cuda.empty_cache()
            params = request["parameters"]
            rate = 24000
            full = np.zeros(round(source_info.duration * rate), dtype=np.float32)
            for original, track in zip(source_meta, tracks):
                rendered, actual_rate = self.core.synthesize_audio(
                    json.dumps([track], ensure_ascii=False), str(reference), prompt_metadata=prompt_meta,
                    control="score", auto_shift=False, use_fp16=False,
                    cfg=params["inference_cfg_rate"], n_steps=params["diffusion_steps"], seed=params["seed"])
                if actual_rate != rate or not np.isfinite(rendered).all():
                    raise RuntimeError("invalid SoulX output audio")
                start = round(original["time"][0] * rate / 1000)
                end = min(len(full), round(original["time"][1] * rate / 1000))
                rendered = rendered.reshape(-1)
                count = min(max(0, end - start), len(rendered))
                full[start:start+count] = rendered[:count]
            sf.write(output.parent / "dry-vocal.wav", full, rate, subtype="FLOAT")
            sf.write(output.parent / "accompaniment.wav", source_stems["accompaniment"], source_stems["sample_rate"], subtype="FLOAT")
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(output.parent / "dry-vocal.wav"), "-i", str(output.parent / "accompaniment.wav"), "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0", "-ar", str(source_stems["sample_rate"]), "-ac", "2", "-c:a", "pcm_f32le", str(output)], check=True, timeout=300)
            mixed, mix_rate = sf.read(output, dtype="float32", always_2d=True)
            peak = float(np.max(np.abs(mixed)))
            if not np.isfinite(mixed).all():
                raise RuntimeError("invalid SoulX mixed audio")
            if peak > 0.98:
                sf.write(output, mixed * (0.98 / peak), mix_rate, subtype="FLOAT")
            torch.cuda.empty_cache()
        return output
