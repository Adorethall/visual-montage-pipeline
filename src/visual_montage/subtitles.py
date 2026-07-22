from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .io import write_json


TIMESTAMPED_SEGMENT = re.compile(
    r"\[(?P<start>\d+(?:\.\d+)?)\]"
    r"\[(?P<speaker>S\d+)\]"
    r"(?P<text>.*?)"
    r"\[(?P<end>\d+(?:\.\d+)?)\]",
    re.DOTALL,
)
SUBTITLE_ALIGNMENT_VERSION = "2"


def _display_units(text: str) -> int:
    return sum(
        2 if ("\u4e00" <= char <= "\u9fff" or ord(char) > 0xFFFF) else 1
        for char in text
    )


def wrap_subtitle_text(text: str, maximum_units: int = 28) -> str:
    """Wrap subtitle text without splitting English words or CJK characters."""
    normalized = " ".join(str(text).strip().split())
    if not normalized or maximum_units <= 0:
        return normalized
    has_word_spaces = " " in normalized
    tokens = normalized.split() if has_word_spaces else list(normalized)
    separator = " " if has_word_spaces else ""
    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = token if not current else f"{current}{separator}{token}"
        if current and _display_units(candidate) > maximum_units:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def _duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def parse_moss_response(raw_response: str) -> dict:
    text = raw_response.strip()
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict):
            text = str(envelope.get("text") or "")
    except json.JSONDecodeError:
        pass
    segments = [
        {
            "start": float(match.group("start")),
            "end": float(match.group("end")),
            "speaker": match.group("speaker"),
            "text": match.group("text").strip(),
        }
        for match in TIMESTAMPED_SEGMENT.finditer(text)
    ]
    return {"raw_text": text, "segments": segments}


def _semantic_chunks(
    text: str,
    maximum_chars: int = 14,
    maximum_words: int = 8,
) -> list[str]:
    stripped = text.strip()
    latin_letters = len(re.findall(r"[A-Za-z]", stripped))
    chinese_chars = sum("\u4e00" <= char <= "\u9fff" for char in stripped)
    if latin_letters > chinese_chars:
        sentences = [
            item.strip()
            for item in re.split(r"(?<=[.!?])\s+", stripped)
            if item.strip()
        ]
        output = []
        for sentence in sentences:
            words = sentence.split()
            output.extend(
                " ".join(words[index:index + maximum_words])
                for index in range(0, len(words), maximum_words)
            )
        return output
    normalized = re.sub(r"\s+", "", stripped)
    primary = [
        item for item in re.split(r"[。！？!?；;]+", normalized) if item
    ]
    output = []
    for sentence in primary:
        clauses = [item for item in re.split(r"[，,、]+", sentence) if item]
        for clause in clauses:
            if len(clause) <= maximum_chars:
                output.append(clause)
                continue
            split_at = len(clause) // 2
            output.extend((clause[:split_at], clause[split_at:]))
    if len(output) >= 2 and len(output[-1]) <= 2:
        output[-2] += output.pop()
    return output


def _weight(text: str) -> float:
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    latin = len(re.findall(r"[A-Za-z]+", text))
    digits = sum(char.isdigit() for char in text)
    return max(1.0, chinese + latin * 1.4 + digits * 1.2)


def detect_silence_boundaries(
    audio_path: Path,
    *,
    minimum_silence_seconds: float = 0.08,
) -> list[float]:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(audio_path),
            "-af",
            f"silencedetect=noise=-35dB:d={minimum_silence_seconds}",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    starts = [
        float(value)
        for value in re.findall(r"silence_start:\s*([0-9.]+)", result.stderr)
    ]
    ends = [
        float(value)
        for value in re.findall(r"silence_end:\s*([0-9.]+)", result.stderr)
    ]
    return [
        round((start + end) / 2, 6)
        for start, end in zip(starts, ends)
        if end > start
    ]


def _choose_silence_boundaries(
    ideal_boundaries: list[float],
    silence_boundaries: list[float],
    audio_seconds: float,
    *,
    minimum_segment_seconds: float = 0.45,
) -> list[float]:
    usable = [
        value
        for value in silence_boundaries
        if minimum_segment_seconds
        <= value
        <= audio_seconds - minimum_segment_seconds
    ]
    selected = []
    previous = 0.0
    for index, ideal in enumerate(ideal_boundaries):
        remaining_segments = len(ideal_boundaries) - index
        maximum = audio_seconds - minimum_segment_seconds * remaining_segments
        candidates = [
            value
            for value in usable
            if previous + minimum_segment_seconds <= value <= maximum
            and value not in selected
        ]
        if candidates:
            boundary = min(candidates, key=lambda value: abs(value - ideal))
        else:
            boundary = max(
                previous + minimum_segment_seconds,
                min(ideal, maximum),
            )
        selected.append(boundary)
        previous = boundary
    return selected


def align_subtitles(
    *,
    source_text: str,
    audio_seconds: float,
    asr_segments: list[dict],
    silence_boundaries: list[float] | None = None,
) -> list[dict]:
    chunks = _semantic_chunks(source_text)
    if not chunks:
        return []
    weights = [_weight(chunk) for chunk in chunks]
    total_weight = sum(weights)
    cumulative = 0.0
    ideal_boundaries = []
    for weight in weights[:-1]:
        cumulative += weight
        ideal_boundaries.append(audio_seconds * cumulative / total_weight)
    boundaries = _choose_silence_boundaries(
        ideal_boundaries,
        silence_boundaries or [],
        audio_seconds,
    )
    points = [0.0, *boundaries, audio_seconds]
    output = []
    for index, chunk in enumerate(chunks):
        start = points[index]
        end = points[index + 1]
        output.append({
            "text": chunk,
            "speaker": (
                str(asr_segments[0].get("speaker") or "S01")
                if asr_segments else "S01"
            ),
            "start": round(start, 3),
            "end": round(end, 3),
        })
    output[0]["start"] = 0.0
    output[-1]["end"] = round(audio_seconds, 3)
    return output


def call_moss_asr(audio_path: Path) -> dict:
    load_dotenv()
    base = os.environ["MOSS_ASR_API_BASE"].rstrip("/")
    endpoint = os.getenv("MOSS_ASR_ENDPOINT", "/audio/transcriptions")
    prompt = (
        "请将音频转写为文本，每一段需以起始时间戳和说话人编号"
        "（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，"
        "并在段末标注结束时间戳，以清晰标明该段语音范围。"
    )
    with audio_path.open("rb") as handle:
        response = httpx.post(
            f"{base}{endpoint}",
            headers={
                "Authorization": f"Bearer {os.environ['MOSS_ASR_API_KEY']}"
            },
            data={
                "model": os.getenv(
                    "MOSS_ASR_MODEL",
                    "OpenMOSS-Team/MOSS-Transcribe-Diarize",
                ),
                "response_format": os.getenv(
                    "MOSS_ASR_RESPONSE_FORMAT", "text"
                ),
                "prompt": prompt,
            },
            files={"file": (audio_path.name, handle, "audio/wav")},
            timeout=300,
        )
    response.raise_for_status()
    return {
        **parse_moss_response(response.text),
        "response": response.text,
        "model_id": os.getenv("MOSS_ASR_MODEL"),
    }


def generate_subtitles(
    *,
    audio_path: Path,
    source_text: str,
    output: Path,
    cache_dir: Path,
    force: bool = False,
) -> dict:
    audio_bytes = audio_path.read_bytes()
    cache_key = hashlib.sha256(
        audio_bytes
        + source_text.encode("utf-8")
        + SUBTITLE_ALIGNMENT_VERSION.encode("utf-8")
    ).hexdigest()[:20]
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.exists() and not force:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["cache_hit"] = True
        write_json(output, payload)
        return payload
    audio_seconds = _duration(audio_path)
    method = "moss_asr_plus_weighted_alignment"
    error = None
    try:
        asr = call_moss_asr(audio_path)
    except Exception as exc:
        asr = {"segments": [], "raw_text": "", "model_id": None}
        method = "weighted_alignment_fallback"
        error = str(exc)
    silence_boundaries = detect_silence_boundaries(audio_path)
    segments = align_subtitles(
        source_text=source_text,
        audio_seconds=audio_seconds,
        asr_segments=asr.get("segments") or [],
        silence_boundaries=silence_boundaries,
    )
    duration_error = (
        abs(float(segments[-1]["end"]) - audio_seconds)
        if segments else audio_seconds
    )
    payload = {
        "schema_version": "1.0",
        "audio_path": str(audio_path.resolve()),
        "audio_seconds": round(audio_seconds, 3),
        "source_text": source_text,
        "alignment_method": (
            f"{method}_plus_silence_boundaries"
            if method.startswith("moss_asr")
            else method
        ),
        "asr_model_id": asr.get("model_id"),
        "asr_raw_text": asr.get("raw_text"),
        "asr_segments": asr.get("segments") or [],
        "silence_boundaries": silence_boundaries,
        "segments": segments,
        "cache_key": cache_key,
        "cache_hit": False,
        "error": error,
        "validation": {
            "passed": bool(segments) and duration_error <= 0.05,
            "duration_error_seconds": round(duration_error, 6),
            "segment_count": len(segments),
            "overlap_count": sum(
                left["end"] > right["start"] + 0.001
                for left, right in zip(segments, segments[1:])
            ),
        },
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_json(cache_path, payload)
    write_json(output, payload)
    return payload
