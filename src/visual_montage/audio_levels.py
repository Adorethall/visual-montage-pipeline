from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path


def measure_volume_db(path: Path) -> dict[str, float]:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(path),
            "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    mean_matches = re.findall(
        r"mean_volume:\s*(-?[0-9.]+) dB", result.stderr
    )
    peak_matches = re.findall(
        r"max_volume:\s*(-?[0-9.]+) dB", result.stderr
    )
    if not mean_matches or not peak_matches:
        raise RuntimeError(f"unable to measure audio volume: {path}")
    return {
        "mean_db": float(mean_matches[-1]),
        "peak_db": float(peak_matches[-1]),
    }


def gain_db_to_volume(gain_db: float) -> float:
    return 10 ** (gain_db / 20.0)


def adaptive_mix_levels(
    voiceover: Path,
    bgm: Path,
    *,
    voice_target_mean_db: float = -16.0,
    voice_peak_ceiling_db: float = -1.0,
    bgm_target_mean_db: float = -20.0,
    voiceover_margin_db: float = 12.0,
) -> dict:
    voice = measure_volume_db(voiceover)
    music = measure_volume_db(bgm)
    voice_gain_db = min(
        voice_target_mean_db - voice["mean_db"],
        voice_peak_ceiling_db - voice["peak_db"],
    )
    voice_output_mean_db = voice["mean_db"] + voice_gain_db
    bgm_gain_db = min(0.0, bgm_target_mean_db - music["mean_db"])
    bgm_output_mean_db = music["mean_db"] + bgm_gain_db
    desired_ducked_mean_db = voice_output_mean_db - voiceover_margin_db
    ducking_db = min(0.0, desired_ducked_mean_db - bgm_output_mean_db)
    return {
        "voiceover": {
            **voice,
            "gain_db": round(voice_gain_db, 3),
            "volume": round(gain_db_to_volume(voice_gain_db), 6),
            "output_mean_db": round(voice_output_mean_db, 3),
        },
        "bgm": {
            **music,
            "gain_db": round(bgm_gain_db, 3),
            "volume": round(gain_db_to_volume(bgm_gain_db), 6),
            "output_mean_db": round(bgm_output_mean_db, 3),
            "voiceover_ducking_db": round(ducking_db, 3),
            "ducked_volume": round(
                gain_db_to_volume(bgm_gain_db + ducking_db), 6
            ),
        },
    }
