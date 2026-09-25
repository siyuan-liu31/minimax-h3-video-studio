"""CPU-only, peak-safe remixing of retained voice stems; originals stay intact."""
from __future__ import annotations

import math
import re
import subprocess
import tempfile
from pathlib import Path

from .errors import ApiError


def mix_parameters(data: dict) -> dict[str, float]:
    keys = {"vocal_gain_db", "accompaniment_gain_db"}
    if not isinstance(data, dict) or set(data) - keys:
        raise ApiError(400, "invalid_parameter", "Expected vocal_gain_db and accompaniment_gain_db")
    result = {}
    for key in keys:
        value = data.get(key, 0)
        if type(value) not in (int, float) or not math.isfinite(value) or not -18 <= value <= 12:
            raise ApiError(400, "invalid_parameter", f"{key} must be a finite number from -18 to 12 dB")
        result[key] = float(value)
    return result


def remix_audio(vocal: Path, backing: Path, output: Path, parameters: dict) -> None:
    parameters = mix_parameters(parameters)
    def run(args):
        return subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-y", *args],
                              capture_output=True, text=True, timeout=180, check=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".remix-", dir=output.parent) as directory:
            raw, final = Path(directory) / "float.wav", Path(directory) / "final.wav"
            graph = (f"[0:a]volume={parameters['vocal_gain_db']}dB[v];"
                     f"[1:a]volume={parameters['accompaniment_gain_db']}dB[b];"
                     "[v][b]amix=inputs=2:duration=longest:normalize=0[out]")
            run(["-i", str(vocal), "-i", str(backing), "-filter_complex", graph,
                 "-map", "[out]", "-c:a", "pcm_f32le", str(raw)])
            # astats reads float samples without clipping at 0 dB as volumedetect does.
            stats = run(["-i", str(raw), "-af", "astats=metadata=0:reset=0", "-f", "null", "-"])
            peaks = re.findall(r"Peak level dB:\s*([-+\d.e]+|[-+]inf)", stats.stderr)
            if not peaks:
                raise ValueError("Missing audio peak measurement")
            peak = max(float(value) for value in peaks)
            attenuation = min(0.0, -0.2 - peak)
            run(["-i", str(raw), "-af", f"volume={attenuation}dB", "-c:a", "pcm_s16le", str(final)])
            final.replace(output)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ApiError(500, "voice_remix_failed", "混音失败，请确认音轨完整后重试") from exc
