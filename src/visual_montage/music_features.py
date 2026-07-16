"""Audio/BGM feature extraction helpers for Rednote materials."""

from __future__ import annotations

import array
import hashlib
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MIN_DURATION_SECONDS = 15.0
DEFAULT_SAMPLE_RATE = 22050
DEFAULT_CURVE_POINTS = 64
DEFAULT_FRAME_SECONDS = 0.093
DEFAULT_HOP_SECONDS = 0.023


def run_ffmpeg_pcm(input_path: Path, sample_rate: int) -> array.array:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("未找到 ffmpeg，无法读取音频")

    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    try:
        proc = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg 读取音频失败: {input_path}\n{detail}") from exc

    samples = array.array("h")
    samples.frombytes(proc.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def save_wav(input_path: Path, output_path: Path, sample_rate: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        str(output_path),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"保存 wav 失败: {output_path}\n{detail}") from exc


def stable_audio_id(path: Path) -> str:
    digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
    return f"{path.stem}_{digest}"


def round_float(value: float, digits: int = 4) -> float:
    if not math.isfinite(value):
        return 0.0
    return round(float(value), digits)


def round_list(values: Iterable[float], digits: int = 3) -> list[float]:
    return [round_float(value, digits) for value in values]


def frame_rms(samples: array.array, frame_size: int, hop_size: int) -> list[float]:
    values: list[float] = []
    max_amp = 32768.0
    total = len(samples)
    if total == 0:
        return values

    for start in range(0, max(1, total - frame_size + 1), hop_size):
        frame = samples[start:start + frame_size]
        if not frame:
            continue
        mean_square = sum((sample / max_amp) ** 2 for sample in frame) / len(frame)
        values.append(math.sqrt(mean_square))
    return values


def smooth(values: list[float], radius: int = 2) -> list[float]:
    if radius <= 0 or not values:
        return values[:]
    output: list[float] = []
    for index in range(len(values)):
        start = max(0, index - radius)
        end = min(len(values), index + radius + 1)
        output.append(sum(values[start:end]) / (end - start))
    return output


def onset_from_energy(rms_values: list[float]) -> list[float]:
    if not rms_values:
        return []
    log_energy = [math.log1p(value * 100.0) for value in rms_values]
    onset = [0.0]
    for previous, current in zip(log_energy, log_energy[1:]):
        onset.append(max(0.0, current - previous))
    return smooth(onset, radius=1)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[int(pos)]
    return ordered[lower] * (upper - pos) + ordered[upper] * (pos - lower)


def normalize(value: float, values: list[float], q: float = 0.95) -> float:
    scale = percentile(values, q)
    if scale <= 1e-9:
        return 0.0
    return round_float(max(0.0, min(1.0, value / scale)))


def estimate_bpm(onset: list[float], sample_rate: int, hop_size: int) -> tuple[float, float]:
    if len(onset) < 4:
        return 0.0, 0.0

    frame_rate = sample_rate / hop_size
    min_bpm = 60.0
    max_bpm = 180.0
    min_lag = max(1, int(frame_rate * 60.0 / max_bpm))
    max_lag = max(min_lag + 1, int(frame_rate * 60.0 / min_bpm))
    centered = [value - (sum(onset) / len(onset)) for value in onset]

    best_lag = 0
    best_score = 0.0
    for lag in range(min_lag, min(max_lag, len(centered) - 1) + 1):
        pairs = zip(centered[:-lag], centered[lag:])
        score = sum(left * right for left, right in pairs)
        if score > best_score:
            best_score = score
            best_lag = lag

    if best_lag <= 0:
        return 0.0, 0.0
    bpm = 60.0 * frame_rate / best_lag
    return bpm, best_score


def local_peak_indices(values: list[float], threshold: float) -> list[int]:
    peaks: list[int] = []
    for index in range(1, len(values) - 1):
        value = values[index]
        if value >= threshold and value >= values[index - 1] and value >= values[index + 1]:
            peaks.append(index)
    return peaks


def estimate_beats(onset: list[float], bpm: float, sample_rate: int, hop_size: int) -> list[float]:
    if bpm <= 0 or not onset:
        return []

    frame_rate = sample_rate / hop_size
    beat_period_frames = max(1, int(round(frame_rate * 60.0 / bpm)))
    threshold = percentile(onset, 0.75)
    peaks = local_peak_indices(onset, threshold)
    if not peaks:
        return []

    beats = [peaks[0]]
    current = peaks[0]
    tolerance = max(2, beat_period_frames // 3)
    while current + beat_period_frames < len(onset):
        target = current + beat_period_frames
        candidates = [peak for peak in peaks if target - tolerance <= peak <= target + tolerance]
        current = max(candidates, key=lambda peak: onset[peak]) if candidates else target
        beats.append(current)

    return [frame * hop_size / sample_rate for frame in beats]


def beat_stability(beats: list[float]) -> float:
    if len(beats) < 4:
        return 0.0
    intervals = [right - left for left, right in zip(beats, beats[1:])]
    mean_interval = sum(intervals) / len(intervals)
    if mean_interval <= 1e-9:
        return 0.0
    variance = sum((value - mean_interval) ** 2 for value in intervals) / len(intervals)
    cv = math.sqrt(variance) / mean_interval
    return round_float(max(0.0, min(1.0, 1.0 - cv)))


def bpm_bucket_and_fit(bpm: float) -> tuple[str, float]:
    if bpm <= 0:
        return "unknown", 0.0
    if 60 <= bpm < 90:
        return "slow_lifestyle_emotion", 0.55
    if 90 <= bpm < 120:
        return "general_seed", 0.82
    if 120 <= bpm <= 150:
        return "fast_mix_ads", 1.0
    if 150 < bpm <= 180:
        return "impact_sport_transition", 0.72
    return "out_of_range", 0.35


def level_from_score(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.38:
        return "medium"
    return "low"


def downsample(values: list[float], points: int) -> list[float]:
    if points <= 0 or len(values) <= points:
        return round_list(values)
    output: list[float] = []
    for index in range(points):
        start = int(index * len(values) / points)
        end = int((index + 1) * len(values) / points)
        chunk = values[start:max(start + 1, end)]
        output.append(sum(chunk) / len(chunk))
    return round_list(output)


def cut_strategy(bpm: float, beat_quality: float, energy: float) -> dict[str, object]:
    bucket, _ = bpm_bucket_and_fit(bpm)
    if beat_quality >= 0.7 and 120 <= bpm <= 150:
        return {"mode": "beat_driven", "cut_every_beats": 1, "recommended_clip_type": "fast_mix_ads"}
    if beat_quality >= 0.55 and 90 <= bpm < 120:
        return {"mode": "beat_driven", "cut_every_beats": 2, "recommended_clip_type": "general_seed"}
    if energy >= 0.72:
        return {"mode": "energy_peak", "cut_every_beats": None, "recommended_clip_type": bucket}
    return {"mode": "soft_bgm", "cut_every_beats": 4, "recommended_clip_type": bucket}


def final_score(raw_score: float, passed_min_duration: bool, voiceover_detected: bool) -> float:
    if not passed_min_duration or voiceover_detected:
        return 0.0
    return raw_score


def apply_voiceover_decision(analysis: dict[str, Any], *, reason: str, source: str) -> dict[str, Any]:
    """Mark an audio analysis as unsuitable BGM because narration was detected."""
    reasons = list(analysis.get("filter_reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    analysis["filter_reasons"] = reasons
    analysis["passed_filters"] = False
    analysis["score"] = 0.0
    analysis["bgm_score"] = 0.0
    analysis["voiceover_source"] = source
    return analysis


def transcript_text_from_json(transcript: dict[str, Any]) -> str:
    text = str(transcript.get("text") or transcript.get("transcript") or "").strip()
    if text:
        return text

    parts: list[str] = []
    segments = transcript.get("segments")
    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, dict):
                value = str(segment.get("text") or "").strip()
                if value:
                    parts.append(value)
    return " ".join(parts).strip()


def asr_voiceover_decision(
    *,
    transcript: dict[str, Any],
    min_text_chars: int,
    min_segments: int,
    local_voiceover: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = transcript_text_from_json(transcript)
    segments = transcript.get("segments")
    segment_count = len(segments) if isinstance(segments, list) else 0
    text_length = len("".join(text.split()))
    vocal_text_detected = text_length >= min_text_chars or (
        min_segments > 0 and segment_count >= min_segments and text_length > 0
    )
    local_voiceover = local_voiceover or {}
    risk_score = float(local_voiceover.get("risk_score") or 0.0)
    reasons = {str(reason).lower() for reason in local_voiceover.get("reasons") or []}
    strong_voiceover_signals = {
        "slow_speech_like_bpm",
        "strong_speech_like_onsets",
        "less_music_like_stability",
    }
    lyric_like = (
        vocal_text_detected
        and risk_score <= 0.35
        and "slow_speech_like_bpm" not in reasons
    )
    detected = vocal_text_detected and not lyric_like and (
        risk_score >= 0.55
        or len(reasons & strong_voiceover_signals) >= 2
        or "slow_speech_like_bpm" in reasons
    )
    return {
        "detected": detected,
        "vocal_text_detected": vocal_text_detected,
        "classification": "voiceover" if detected else ("lyrics_or_vocal_bgm" if lyric_like else "no_voiceover"),
        "text_length": text_length,
        "segment_count": segment_count,
        "sample_text": text[:120],
        "min_text_chars": min_text_chars,
        "min_segments": min_segments,
        "local_voiceover_risk_score": risk_score,
        "local_voiceover_reasons": sorted(reasons),
        "method": "asr_transcript_plus_local_music_features",
    }


def voiceover_risk(
    *,
    duration: float,
    bpm: float,
    energy_score: float,
    beat_quality_score: float,
    stability_score: float,
) -> dict[str, object]:
    """Estimate whether rhythmic evidence is likely coming from narration.

    This is a conservative local heuristic. It catches common vlog/口播 clips
    where speech cadence creates strong onsets but the track is not useful BGM.
    Model-based ASR/YAMNet detection can replace or augment this later.
    """
    risk = 0.0
    reasons: list[str] = []

    if duration >= 60:
        risk += 0.25
        reasons.append("long_audio")
    if bpm and bpm < 90:
        risk += 0.25
        reasons.append("slow_speech_like_bpm")
    if beat_quality_score >= 0.85:
        risk += 0.20
        reasons.append("strong_speech_like_onsets")
    if 0.35 <= energy_score <= 0.82:
        risk += 0.15
        reasons.append("voice_level_energy")
    if duration >= 60 and stability_score < 0.9:
        risk += 0.15
        reasons.append("less_music_like_stability")

    risk = round_float(min(1.0, risk))
    detected = risk >= 0.7
    return {
        "detected": detected,
        "risk_score": risk,
        "penalty": 1.0 if detected else 0.0,
        "reasons": reasons,
        "method": "local_energy_onset_heuristic",
    }


def analyze_audio(
    input_path: str | Path,
    *,
    audio_id: str = "",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    min_duration_seconds: float = DEFAULT_MIN_DURATION_SECONDS,
    curve_points: int = DEFAULT_CURVE_POINTS,
    frame_seconds: float = DEFAULT_FRAME_SECONDS,
    hop_seconds: float = DEFAULT_HOP_SECONDS,
    keep_audio: str | Path = "",
) -> dict[str, object]:
    """Extract BGM features from a local video/audio path."""
    source_path = Path(input_path).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"音视频不存在: {source_path}")

    samples = run_ffmpeg_pcm(source_path, sample_rate)
    duration = len(samples) / sample_rate if sample_rate else 0.0

    frame_size = int(frame_seconds * sample_rate)
    hop_size = int(hop_seconds * sample_rate)
    rms_values = frame_rms(samples, max(1, frame_size), max(1, hop_size))
    onset = onset_from_energy(rms_values)

    bpm, _ = estimate_bpm(onset, sample_rate, max(1, hop_size))
    beats = estimate_beats(onset, bpm, sample_rate, max(1, hop_size))
    beat_frame_indices = [min(len(onset) - 1, max(0, int(beat * sample_rate / max(1, hop_size)))) for beat in beats]
    beat_onsets = [onset[index] for index in beat_frame_indices] if onset else []

    mean_rms = sum(rms_values) / len(rms_values) if rms_values else 0.0
    energy_score = normalize(mean_rms, rms_values)
    raw_beat_quality = sum(beat_onsets) / len(beat_onsets) if beat_onsets else 0.0
    beat_quality_score = normalize(raw_beat_quality, onset)
    stability_score = beat_stability(beats)
    bpm_bucket, bpm_fit = bpm_bucket_and_fit(bpm)
    passed_min_duration = duration >= min_duration_seconds
    raw_score = round_float(
        0.4 * beat_quality_score + 0.25 * energy_score + 0.2 * stability_score + 0.15 * bpm_fit
    )
    voiceover = voiceover_risk(
        duration=duration,
        bpm=bpm,
        energy_score=energy_score,
        beat_quality_score=beat_quality_score,
        stability_score=stability_score,
    )
    filter_reasons = [] if passed_min_duration else [f"duration_below_{min_duration_seconds:g}s"]
    if voiceover["detected"]:
        filter_reasons.append("likely_voiceover")
    passed_filters = passed_min_duration and not bool(voiceover["detected"])
    bgm_score = final_score(raw_score, passed_min_duration, bool(voiceover["detected"]))

    audio_path = source_path
    if keep_audio:
        audio_path = Path(keep_audio)
        save_wav(source_path, audio_path, sample_rate)

    return {
        "audio_id": audio_id or stable_audio_id(source_path),
        "source_path": str(source_path),
        "audio_path": str(audio_path),
        "sample_rate": sample_rate,
        "duration_seconds": round_float(duration, 3),
        "passed_filters": passed_filters,
        "filter_reasons": filter_reasons,
        "bpm": round_float(bpm, 3),
        "bpm_bucket": bpm_bucket,
        "beats": round_list(beats),
        "energy": {
            "score": energy_score,
            "level": level_from_score(energy_score),
            "raw_mean_rms": round_float(mean_rms, 6),
        },
        "beat_quality": {
            "score": beat_quality_score,
            "level": level_from_score(beat_quality_score),
            "raw_mean_onset_at_beats": round_float(raw_beat_quality, 6),
        },
        "stability": {"score": stability_score},
        "bpm_fit": {"score": bpm_fit},
        "voiceover": voiceover,
        "asr_voiceover": {
            "enabled": False,
            "status": "not_run",
            "detected": False,
        },
        "onset_strength": downsample(onset, curve_points),
        "energy_curve": downsample(rms_values, curve_points),
        "cut_strategy": cut_strategy(bpm, beat_quality_score, energy_score),
        "raw_score": raw_score,
        "score": bgm_score,
        "bgm_score": bgm_score,
        "score_formula": "0.4 * beat_quality + 0.25 * energy + 0.2 * stability + 0.15 * bpm_fit",
        "implementation_note": "BPM/onset are estimated with ffmpeg PCM + stdlib energy/onset analysis.",
    }
