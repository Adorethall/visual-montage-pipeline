from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat


def _candidate_rank(candidate: dict, preferred: list[str]) -> tuple[float, ...]:
    scores = candidate.get("scores") or {}
    event = str(candidate.get("event") or "")
    event_score = (
        1.0 - preferred.index(event) * 0.08 if event in preferred else 0.55
    )
    return (
        event_score,
        float(scores.get("aesthetic") or 0),
        float(scores.get("subject_visibility") or 0),
        float(scores.get("sharpness") or 0),
        float(scores.get("composition") or 0),
        float(scores.get("payoff") or 0),
    )


def _sample_timestamps(candidate: dict, count: int) -> list[float]:
    trim = candidate.get("preferred_trim") or candidate.get("source_window") or {}
    start = float(trim.get("start") or 0)
    end = float(trim.get("end") or start)
    if end <= start:
        return [float(candidate.get("peak_time") or start)]
    return [start + (end - start) * (index + 1) / (count + 1) for index in range(count)]


def _extract_frame(video: Path, timestamp: float, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1",
            "-vf", "scale=270:480:force_original_aspect_ratio=increase,crop=270:480",
            str(output),
        ],
        check=True,
    )


def _dhash(path: Path) -> str:
    with Image.open(path) as image:
        gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(gray.getdata())
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
    return f"{bits:016x}"


def _hash_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _frame_quality(path: Path) -> tuple[bool, dict[str, float]]:
    with Image.open(path) as image:
        gray = image.convert("L").resize((180, 320), Image.Resampling.BILINEAR)
        pixels = list(gray.getdata())
        highlight_ratio = sum(value >= 245 for value in pixels) / len(pixels)
        shadow_ratio = sum(value <= 10 for value in pixels) / len(pixels)
        mean = float(ImageStat.Stat(gray).mean[0])
        edge_variance = float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0])
    metrics = {
        "highlight_ratio": round(highlight_ratio, 4),
        "shadow_ratio": round(shadow_ratio, 4),
        "mean_luma": round(mean, 2),
        "edge_variance": round(edge_variance, 2),
    }
    acceptable = (
        highlight_ratio < 0.38
        and shadow_ratio < 0.72
        and 18.0 < mean < 238.0
        and edge_variance >= 28.0
    )
    return acceptable, metrics


def _json_content(value: str) -> dict[str, Any]:
    cleaned = value.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def select_cover_frame(
    *,
    selected: list[dict],
    profile: dict,
    output_dir: Path,
    timeout: float = 120.0,
    excluded_hashes: list[str] | None = None,
) -> dict[str, Any]:
    config = profile.get("cover_selection") or {}
    preferred = list(profile.get("cover_events") or [])
    ranked = sorted(
        selected,
        key=lambda candidate: _candidate_rank(candidate, preferred),
        reverse=True,
    )[: max(1, int(config.get("max_candidates", 5)))]
    fallback = ranked[0]
    fallback_timestamp = float(
        fallback.get("peak_time") or fallback["preferred_trim"]["start"]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "cover-frame-options"
    frames_dir.mkdir(parents=True, exist_ok=True)
    options: list[dict[str, Any]] = []
    samples = max(2, int(config.get("samples_per_candidate", 3)))
    for candidate in ranked:
        for timestamp in _sample_timestamps(candidate, samples):
            option_id = f"F{len(options) + 1:02d}"
            frame = frames_dir / f"{option_id}.jpg"
            try:
                _extract_frame(Path(candidate["video_path"]), timestamp, frame)
            except subprocess.CalledProcessError:
                continue
            acceptable, quality = _frame_quality(frame)
            frame_hash = _dhash(frame)
            duplicate = any(
                _hash_distance(frame_hash, prior) <= int(config.get("dedupe_hash_distance", 10))
                for prior in (excluded_hashes or [])
            )
            options.append({
                "id": option_id,
                "frame": frame,
                "candidate": candidate,
                "timestamp": round(timestamp, 3),
                "hash": frame_hash,
                "quality": quality,
                "acceptable": acceptable,
                "duplicate": duplicate,
            })
    if not options:
        return {
            "candidate": fallback,
            "timestamp": fallback_timestamp,
            "mode": "no_review_frames_fallback",
        }

    eligible_options = [
        option for option in options
        if option["acceptable"] and not option["duplicate"]
    ]
    if not eligible_options:
        eligible_options = [option for option in options if not option["duplicate"]]
    if not eligible_options:
        eligible_options = options
    options = eligible_options

    if not bool(config.get("gemma_enabled", True)):
        chosen = options[0]
        return {
            "candidate": chosen["candidate"],
            "timestamp": chosen["timestamp"],
            "frame_hash": chosen["hash"],
            "quality": chosen["quality"],
            "mode": "quality_filtered_fallback",
        }

    columns = 3
    cell_width, cell_height, label_height = 270, 480, 30
    rows = (len(options) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_width, rows * (cell_height + label_height)), "#111111")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    for index, option in enumerate(options):
        x = (index % columns) * cell_width
        y = (index // columns) * (cell_height + label_height)
        with Image.open(option["frame"]) as image:
            sheet.paste(image.convert("RGB"), (x, y))
        draw.rectangle((x, y + cell_height, x + cell_width, y + cell_height + label_height), fill="#111111")
        draw.text((x + 8, y + cell_height + 4), option["id"], fill="white", font=font)
    contact_sheet = output_dir / "cover-frame-options.jpg"
    sheet.save(contact_sheet, "JPEG", quality=88, optimize=True)

    key = os.getenv("LLM_API_KEY", "")
    base = os.getenv("LLM_API_BASE", "").rstrip("/")
    if not key or not base:
        chosen = options[0]
        return {
            "candidate": chosen["candidate"],
            "timestamp": chosen["timestamp"],
            "frame_hash": chosen["hash"],
            "quality": chosen["quality"],
            "mode": "missing_api_config_fallback",
            "contact_sheet": str(contact_sheet),
        }
    encoded = base64.b64encode(contact_sheet.read_bytes()).decode("ascii")
    prompt = """Choose the single strongest source frame for a vertical social-video cover.
Prioritize: a recognizable sharp main subject; an intentional expression, pose, action, product view, or story moment; clean composition; and usable negative space for a headline. Reject overexposure, blown highlights, black frames, motion blur, transition flashes, awkward anatomy, closed eyes, intrusive subtitles, or cropped key features.
Do not prefer an event label over actual visual quality. Return JSON only: {\"choice\":\"F01\",\"reason\":\"short reason\"}."""
    try:
        response = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": os.getenv("GEMMA_MODEL", "google/gemma-4-12B-it"),
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                    ],
                }],
                "temperature": 0.1,
                "max_tokens": 300,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        decision = _json_content(content)
        chosen = next(item for item in options if item["id"] == decision.get("choice"))
        return {
            "candidate": chosen["candidate"],
            "timestamp": chosen["timestamp"],
            "frame_hash": chosen["hash"],
            "quality": chosen["quality"],
            "mode": "gemma_contact_sheet",
            "choice": chosen["id"],
            "reason": str(decision.get("reason") or ""),
            "contact_sheet": str(contact_sheet),
        }
    except Exception as exc:
        chosen = options[0]
        return {
            "candidate": chosen["candidate"],
            "timestamp": chosen["timestamp"],
            "frame_hash": chosen["hash"],
            "quality": chosen["quality"],
            "mode": "gemma_review_failed_fallback",
            "error": str(exc),
            "contact_sheet": str(contact_sheet),
        }
