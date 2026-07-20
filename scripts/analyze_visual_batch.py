#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import mimetypes
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from visual_montage.io import load_manifest, load_yaml, write_json
from visual_montage.candidate_registry import CandidateRegistry
from visual_montage.audio_bgm import analyze_video_bgm
from visual_montage.marlin_routing import marlin_segment_windows, offset_marlin_result
from visual_montage.models import VisualCandidate
from visual_montage.storage import get_storage


ANALYSIS_CACHE_VERSION = "4"
PROXY_SETTINGS = {
    "fps": 3,
    "width": 480,
    "video_codec": "libx264",
    "preset": "veryfast",
    "crf": 34,
    "audio": False,
}


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode())
    digest.update(str(stat.st_mtime_ns).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            handle.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()


def analysis_cache_identity(
    *,
    video: Path,
    video_id: str,
    category: str,
    profile: dict,
    prompt_template: str,
    project_root: Path,
) -> dict:
    marlin = profile.get("marlin_recall") or {}
    marlin_base = {
        key: value
        for key, value in marlin.items()
        if key not in {
            "maximum_video_duration_seconds",
            "segment_duration_seconds",
            "segment_overlap_seconds",
        }
    }
    duration = probe_duration(video)
    maximum_marlin_duration = float(
        marlin.get("maximum_video_duration_seconds", 120.0)
    )
    query_payload = {}
    query_path_value = marlin.get("query_path")
    if query_path_value:
        query_path = project_root / str(query_path_value)
        if query_path.exists():
            query_payload = load_yaml(query_path)
    model_id = os.getenv("GEMMA_MODEL", "google/gemma-4-12B-it")
    source = source_fingerprint(video)
    gemma_review = profile.get("gemma_review") or {}
    gemma_review_base = {
        key: value
        for key, value in gemma_review.items()
        if key not in {
            "api_max_attempts",
            "api_retry_delays_seconds",
            "api_max_concurrency",
        }
    }
    analysis_profile = {
        "gemma_review": gemma_review_base,
        "marlin_recall": marlin_base,
    }
    if duration > maximum_marlin_duration:
        analysis_profile["marlin_long_video_segmentation"] = {
            "maximum_video_duration_seconds": maximum_marlin_duration,
            "segment_duration_seconds": float(
                marlin.get("segment_duration_seconds", 115.0)
            ),
            "segment_overlap_seconds": float(
                marlin.get("segment_overlap_seconds", 5.0)
            ),
        }
    configuration = sha256_json(
        {
            "analysis_cache_version": ANALYSIS_CACHE_VERSION,
            "category": category,
            "analysis_profile": analysis_profile,
            "prompt_template": prompt_template,
            "marlin_queries": query_payload,
            "model_id": model_id,
            "proxy": PROXY_SETTINGS,
        }
    )
    cache_key = hashlib.sha256(
        f"{video_id}|{source}|{configuration}".encode("utf-8")
    ).hexdigest()
    return {
        "cache_key": cache_key,
        "source_fingerprint": source,
        "configuration_fingerprint": configuration,
        "model_id": model_id,
    }


def render_prompt(
    template: str,
    profile: dict,
    video_id: str,
    duration: float,
    marlin_recall: list[dict] | None = None,
) -> str:
    review = profile["gemma_review"]
    events = review["events"]
    duration_rule = review["candidate_duration"]
    count_rule = review["candidate_count"]
    event_catalog = "\n".join(
        f"- {event_id}: {event['label']}; keywords: {', '.join(event.get('keywords') or [])}"
        for event_id, event in events.items()
    )
    replacements = {
        "event_catalog": event_catalog,
        "positive_visual_keywords": "\n".join(
            f"- {value}" for value in review["positive_visual_keywords"]
        ),
        "negative_visual_keywords": "\n".join(
            f"- {value}" for value in review["negative_visual_keywords"]
        ),
        "video_id": video_id,
        "duration": f"{duration:.3f}",
        "candidate_count_min": str(count_rule["minimum"]),
        "candidate_count_max": str(count_rule["maximum"]),
        "candidate_duration_min": str(duration_rule["minimum"]),
        "candidate_duration_max": str(duration_rule["maximum"]),
        "marlin_recall": (
            json.dumps(marlin_recall, ensure_ascii=False, indent=2)
            if marlin_recall
            else "Marlin was not used for this video. Review the complete video directly."
        ),
    }
    output = template
    for key, value in replacements.items():
        output = output.replace("{{" + key + "}}", value)
    return output


def should_use_marlin(profile: dict, duration: float) -> bool:
    config = profile.get("marlin_recall") or {}
    return bool(config.get("enabled")) and duration >= float(
        config.get("minimum_video_duration_seconds", 30.0)
    )


def cut_marlin_segment(proxy: Path, output: Path, start: float, end: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(proxy),
            "-t", f"{end - start:.3f}",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "34",
            "-movflags", "+faststart", str(output),
        ],
        check=True,
    )


def load_marlin_queries(project_root: Path, profile: dict) -> list[dict]:
    config = profile["marlin_recall"]
    query_file = project_root / config["query_path"]
    groups = (load_yaml(query_file).get("query_groups") or [])[
        : int(config.get("max_query_groups", 4))
    ]
    variant = config.get("query_variant", "normal")
    output = []
    for group in groups:
        query = group.get(variant) or group.get("normal") or group.get("broad")
        if query:
            output.append({"query_group": group["id"], "query": query})
    return output


def call_marlin(
    proxy: Path,
    material,
    profile: dict,
    project_root: Path,
    duration: float,
) -> dict:
    from worker_stubs.marlin import MarlinFindInput, marlin_find_stub

    config = profile["marlin_recall"]
    digest = hashlib.sha1(str(material.path).encode("utf-8")).hexdigest()[:12]
    windows = marlin_segment_windows(duration, profile)
    segmented = len(windows) > 1
    results = []
    uploads = []
    segment_dir = proxy.parent / "marlin-segments" / material.video_id
    for segment_index, (start, end) in enumerate(windows, 1):
        segment_path = proxy
        if segmented:
            segment_path = segment_dir / f"segment-{segment_index:03d}.mp4"
            cut_marlin_segment(proxy, segment_path, start, end)
        key = (
            f"{str(config.get('upload_prefix', 'visual-montage/marlin-inputs')).strip('/')}/"
            f"{material.video_id}-{digest}-s{segment_index:03d}.mp4"
        )
        uploaded = get_storage().upload_for_worker(
            segment_path,
            key,
            int(config.get("presigned_url_expires_seconds", 86400)),
        )
        uploads.append({
            **uploaded,
            "segment_index": segment_index,
            "source_start": start,
            "source_end": end,
        })
        for query in load_marlin_queries(project_root, profile):
            result = marlin_find_stub.run(
                input=MarlinFindInput(
                    video_url=uploaded["public_url"],
                    event=query["query"],
                    temperature=0.1,
                )
            )
            results.append(
                {
                    **query,
                    "segment_index": segment_index,
                    "segment_source_start": start,
                    "segment_source_end": end,
                    "ok": result.ok,
                    "scene": result.scene,
                    "events": offset_marlin_result(result.events, start),
                    "span": offset_marlin_result(result.span, start),
                    "raw": result.raw,
                    "status": result.status,
                }
            )
    return {
        "segmented": segmented,
        "segment_count": len(windows),
        "segments": uploads,
        "queries": results,
    }


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def compress(path: Path, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path), "-vf", "fps=3,scale=480:-2", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "34",
            "-movflags", "+faststart", str(output),
        ],
        check=True,
    )


def extract_candidate_frame(
    video: Path,
    peak_time: float,
    output: Path,
    width: int,
    height: int,
) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{peak_time:.3f}", "-i", str(video),
            "-frames:v", "1",
            "-vf",
            (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            ),
            str(output),
        ],
        check=True,
    )


def generate_contact_sheet(
    candidates: list[dict],
    output: Path,
    profile: dict,
) -> dict:
    config = profile.get("contact_sheet") or {}
    if not config.get("enabled", False):
        return {"enabled": False, "generated": False}
    if not candidates:
        return {"enabled": True, "generated": False, "reason": "no_candidates"}

    columns = max(1, int(config.get("columns", 4)))
    thumb_width = int(config.get("thumbnail_width", 320))
    thumb_height = int(config.get("thumbnail_height", 480))
    label_height = int(config.get("label_height", 86))
    rows = (len(candidates) + columns - 1) // columns
    background = str(config.get("background_color", "#151515"))
    primary = str(config.get("label_color", "#F5F5F5"))
    secondary = str(config.get("secondary_label_color", "#B8B8B8"))
    canvas = Image.new(
        "RGB",
        (columns * thumb_width, rows * (thumb_height + label_height)),
        background,
    )
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=18)
    small_font = ImageFont.load_default(size=15)
    failures = []

    with tempfile.TemporaryDirectory(prefix="visual-montage-contact-sheet-") as tmp:
        temp_dir = Path(tmp)
        for index, candidate in enumerate(candidates):
            row, column = divmod(index, columns)
            x = column * thumb_width
            y = row * (thumb_height + label_height)
            frame = temp_dir / f"{index:04d}.jpg"
            try:
                extract_candidate_frame(
                    Path(candidate["video_path"]),
                    float(candidate["peak_time"]),
                    frame,
                    thumb_width,
                    thumb_height,
                )
                with Image.open(frame) as image:
                    canvas.paste(image.convert("RGB"), (x, y))
            except Exception as exc:
                failures.append(
                    {"candidate_id": candidate["candidate_id"], "error": str(exc)}
                )
                draw.rectangle(
                    (x, y, x + thumb_width, y + thumb_height),
                    fill="#303030",
                )
                draw.text((x + 12, y + 12), "FRAME EXTRACTION FAILED", fill=primary, font=font)

            confidence = float(candidate.get("confidence") or 0.0)
            video_id = str(candidate["video_id"])
            short_video_id = video_id if len(video_id) <= 12 else video_id[-12:]
            line_1 = f"{index + 1:02d}  {candidate['event']}"
            line_2 = (
                f"src=...{short_video_id}  t={float(candidate['peak_time']):.2f}s  "
                f"conf={confidence:.2f}"
            )
            draw.text((x + 10, y + thumb_height + 8), line_1, fill=primary, font=font)
            draw.text(
                (x + 10, y + thumb_height + 42),
                line_2,
                fill=secondary,
                font=small_font,
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(
        output,
        "JPEG",
        quality=int(config.get("jpeg_quality", 88)),
        optimize=True,
    )
    return {
        "enabled": True,
        "generated": True,
        "path": str(output),
        "candidate_count": len(candidates),
        "frame_failure_count": len(failures),
        "frame_failures": failures,
        "columns": columns,
        "rows": rows,
        "width": canvas.width,
        "height": canvas.height,
    }


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def parse_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines)
    return json.loads(text)


def call_gemma(
    video: Path,
    prompt_text: str,
    timeout: float,
    max_attempts: int = 3,
    retry_delays: tuple[float, ...] = (2.0, 5.0),
) -> tuple[dict, dict]:
    base = os.environ["LLM_API_BASE"].rstrip("/")
    key = os.environ["LLM_API_KEY"]
    model = os.getenv("GEMMA_MODEL", "google/gemma-4-12B-it")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return valid JSON only. Follow the supplied category review specification exactly.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "video_url", "video_url": {"url": data_url(video)}},
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": 5000,
    }
    attempts = max(1, int(max_attempts))
    errors = []
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            envelope = response.json()
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Gemma returned empty response content")
            return parse_json(content), envelope
        except Exception as exc:
            retryable = isinstance(
                exc,
                (
                    httpx.TransportError,
                    json.JSONDecodeError,
                    KeyError,
                    IndexError,
                    TypeError,
                    AttributeError,
                ),
            )
            if isinstance(exc, httpx.HTTPStatusError):
                retryable = exc.response.status_code in {
                    408, 429, 500, 502, 503, 504,
                }
            message = str(exc)
            if isinstance(exc, ValueError) and any(
                marker in message.lower()
                for marker in ("empty response", "expecting value")
            ):
                retryable = True
            errors.append({
                "attempt": attempt,
                "type": type(exc).__name__,
                "error": message,
                "retryable": retryable,
            })
            if not retryable or attempt >= attempts:
                raise RuntimeError(
                    f"Gemma request failed after {attempt} attempt(s): "
                    f"{message}; attempts={errors}"
                ) from exc
            delay = (
                retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                if retry_delays else 0.0
            )
            print(
                f"Gemma transient failure on attempt {attempt}/{attempts}: "
                f"{message}; retrying in {delay:g}s",
                flush=True,
            )
            if delay > 0:
                time.sleep(delay)


def build_marlin_review_windows(
    marlin: dict,
    duration: float,
    profile: dict,
) -> list[dict]:
    """Turn absolute Marlin recalls into small, bounded Gemma review windows."""
    config = profile.get("marlin_recall") or {}
    context = max(0.0, float(config.get("gemma_review_context_seconds", 1.5)))
    max_window = max(
        1.0, float(config.get("gemma_review_max_window_seconds", 12.0))
    )
    max_windows = max(1, int(config.get("gemma_review_max_windows", 8)))
    max_recall_span = max(
        max_window,
        float(config.get("gemma_review_max_recall_span_seconds", 30.0)),
    )

    precise = []
    broad = []
    for query in marlin.get("queries") or []:
        if query.get("ok") is False:
            continue
        recalls = query.get("events") or []
        if not recalls and isinstance(query.get("span"), dict):
            recalls = [query["span"]]
        if isinstance(recalls, dict):
            recalls = [recalls]
        for recall in recalls:
            if not isinstance(recall, dict):
                continue
            try:
                start = max(0.0, min(duration, float(recall["start"])))
                end = max(start, min(duration, float(recall["end"])))
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                continue
            item = {
                "start": start,
                "end": end,
                "query_group": query.get("query_group"),
                "description": recall.get("description") or recall.get("label") or "",
            }
            (precise if end - start <= max_recall_span else broad).append(item)

    recalls = precise or broad
    windows = []
    for recall in recalls:
        start = max(0.0, recall["start"] - context)
        end = min(duration, recall["end"] + context)
        if end - start > max_window:
            center = (recall["start"] + recall["end"]) / 2
            start = max(0.0, center - max_window / 2)
            end = min(duration, start + max_window)
            start = max(0.0, end - max_window)
        windows.append({
            "start": start,
            "end": end,
            "query_groups": [recall["query_group"]] if recall["query_group"] else [],
            "descriptions": [recall["description"]] if recall["description"] else [],
        })

    windows.sort(key=lambda item: (item["start"], item["end"]))
    merged = []
    for window in windows:
        if (
            merged
            and window["start"] <= merged[-1]["end"]
            and max(merged[-1]["end"], window["end"]) - merged[-1]["start"] <= max_window
        ):
            current = merged[-1]
            current["end"] = max(current["end"], window["end"])
            current["query_groups"] = list(dict.fromkeys(
                current["query_groups"] + window["query_groups"]
            ))
            current["descriptions"] = list(dict.fromkeys(
                current["descriptions"] + window["descriptions"]
            ))
        elif not merged or (
            abs(window["start"] - merged[-1]["start"]) > 0.01
            or abs(window["end"] - merged[-1]["end"]) > 0.01
        ):
            merged.append(window)

    return [
        {
            **window,
            "start": round(window["start"], 3),
            "end": round(window["end"], 3),
        }
        for window in merged[:max_windows]
    ]


def offset_gemma_analysis(analysis: dict, window_start: float, window_end: float) -> dict:
    """Convert Gemma clip-relative candidate timestamps to source timestamps."""
    output = copy.deepcopy(analysis)
    clip_duration = max(0.0, window_end - window_start)
    converted = []
    for item in output.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        try:
            relative_start = max(0.0, min(clip_duration, float(item["start"])))
            relative_end = max(relative_start, min(clip_duration, float(item["end"])))
        except (KeyError, TypeError, ValueError):
            continue
        adjusted = dict(item)
        adjusted["start"] = round(window_start + relative_start, 3)
        adjusted["end"] = round(window_start + relative_end, 3)
        try:
            relative_peak = float(item.get("peak_time", (relative_start + relative_end) / 2))
        except (TypeError, ValueError):
            relative_peak = (relative_start + relative_end) / 2
        relative_peak = max(relative_start, min(relative_end, relative_peak))
        adjusted["peak_time"] = round(window_start + relative_peak, 3)
        adjusted["gemma_review_window"] = {
            "source_start": round(window_start, 3),
            "source_end": round(window_end, 3),
        }
        converted.append(adjusted)
    output["candidates"] = converted
    return output


def call_gemma_on_marlin_windows(
    proxy: Path,
    material,
    profile: dict,
    marlin: dict,
    prompt_text: str,
    duration: float,
    timeout: float,
    checkpoint_namespace: str = "primary",
) -> tuple[dict, dict, dict]:
    windows = build_marlin_review_windows(marlin, duration, profile)
    if not windows:
        raise RuntimeError("Marlin returned no reviewable candidate windows")

    review_dir = proxy.parent / "gemma-review-windows" / material.video_id
    review_config = profile.get("gemma_review") or {}
    max_attempts = int(review_config.get("api_max_attempts", 3))
    max_concurrency = max(
        1, min(5, int(review_config.get("api_max_concurrency", 5)))
    )
    retry_delays = tuple(
        float(value)
        for value in review_config.get("api_retry_delays_seconds", [2.0, 5.0])
    )
    combined = {"candidates": [], "video_summary": "", "rejected_patterns": []}
    usages = []
    reviewed = []

    def review_window(index_and_window: tuple[int, dict]) -> tuple[dict, dict, dict]:
        index, window = index_and_window
        clip = review_dir / f"review-{index:03d}.mp4"
        clip_duration = window["end"] - window["start"]
        window_prompt = (
            f"{prompt_text}\n\n"
            "IMPORTANT REVIEW-WINDOW RULES:\n"
            f"- The attached video is only a {clip_duration:.3f}-second excerpt from "
            f"original source time {window['start']:.3f}-{window['end']:.3f}s.\n"
            "- Review only this attached excerpt.\n"
            f"- Return start, end, and peak_time relative to this excerpt, from 0 to {clip_duration:.3f}.\n"
            "- Do not return original-source absolute timestamps."
        )
        checkpoint_suffix = (
            "" if checkpoint_namespace == "primary"
            else f"-{checkpoint_namespace}"
        )
        checkpoint = review_dir / f"review-{index:03d}{checkpoint_suffix}.json"
        checkpoint_key = sha256_json({
            "video_id": material.video_id,
            "window": {"start": window["start"], "end": window["end"]},
            "prompt": window_prompt,
            "model": os.getenv("GEMMA_MODEL", "google/gemma-4-12B-it"),
            "cache_version": ANALYSIS_CACHE_VERSION,
        })
        cached_window = None
        if checkpoint.exists():
            try:
                candidate_checkpoint = json.loads(
                    checkpoint.read_text(encoding="utf-8")
                )
                if candidate_checkpoint.get("checkpoint_key") == checkpoint_key:
                    cached_window = candidate_checkpoint
            except (OSError, json.JSONDecodeError):
                cached_window = None
        if cached_window:
            adjusted = cached_window["analysis"]
            envelope = {"usage": cached_window.get("usage")}
            checkpoint_hit = True
        else:
            cut_marlin_segment(proxy, clip, window["start"], window["end"])
            analysis, envelope = call_gemma(
                clip,
                window_prompt,
                timeout,
                max_attempts=max_attempts,
                retry_delays=retry_delays,
            )
            adjusted = offset_gemma_analysis(
                analysis, window["start"], window["end"]
            )
            write_json(checkpoint, {
                "checkpoint_key": checkpoint_key,
                "window": {"start": window["start"], "end": window["end"]},
                "analysis": adjusted,
                "usage": envelope.get("usage"),
            })
            checkpoint_hit = False
        metadata = {
            **window,
            "clip_path": str(clip),
            "clip_duration_seconds": round(clip_duration, 3),
            "candidate_count": len(adjusted.get("candidates") or []),
            "checkpoint_hit": checkpoint_hit,
        }
        return adjusted, envelope, metadata

    with ThreadPoolExecutor(
        max_workers=min(max_concurrency, len(windows)),
        thread_name_prefix="gemma-review",
    ) as executor:
        window_results = list(
            executor.map(review_window, enumerate(windows, 1))
        )

    for adjusted, envelope, metadata in window_results:
        combined["candidates"].extend(adjusted.get("candidates") or [])
        if adjusted.get("video_summary"):
            combined["video_summary"] = " ".join(filter(None, [
                combined["video_summary"], str(adjusted["video_summary"])
            ]))
        combined["rejected_patterns"].extend(adjusted.get("rejected_patterns") or [])
        usages.append(envelope.get("usage"))
        reviewed.append(metadata)
    return combined, {"usage": {"request_count": len(usages), "requests": usages}}, {
        "mode": "marlin_candidate_windows",
        "checkpoint_namespace": checkpoint_namespace,
        "max_concurrency": max_concurrency,
        "window_count": len(reviewed),
        "windows": reviewed,
    }


def merge_gemma_analyses(base: dict, extra: dict) -> dict:
    output = copy.deepcopy(base)
    output.setdefault("candidates", []).extend(
        copy.deepcopy(extra.get("candidates") or [])
    )
    if extra.get("video_summary"):
        output["video_summary"] = " ".join(filter(None, [
            str(output.get("video_summary") or ""),
            str(extra["video_summary"]),
        ]))
    output.setdefault("rejected_patterns", []).extend(
        copy.deepcopy(extra.get("rejected_patterns") or [])
    )
    return output


def deduplicate_candidates(candidates: list[dict]) -> list[dict]:
    """Remove repeated model discoveries while keeping the strongest candidate."""
    ranked = sorted(
        candidates,
        key=lambda item: float(item.get("confidence") or 0.0),
        reverse=True,
    )
    kept = []
    for candidate in ranked:
        start = float(candidate["source_window"]["start"])
        end = float(candidate["source_window"]["end"])
        duplicate = False
        for existing in kept:
            other_start = float(existing["source_window"]["start"])
            other_end = float(existing["source_window"]["end"])
            intersection = max(0.0, min(end, other_end) - max(start, other_start))
            shorter = max(0.001, min(end - start, other_end - other_start))
            if intersection / shorter >= 0.75:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return sorted(
        kept,
        key=lambda item: float(item["source_window"]["start"]),
    )


def candidate_topup_prompt(
    prompt_text: str,
    candidates: list[dict],
    minimum: int,
    attempt: int,
) -> str:
    existing = [
        {
            "start": item["source_window"]["start"],
            "end": item["source_window"]["end"],
            "event": item.get("event"),
        }
        for item in candidates
    ]
    missing = max(1, minimum - len(candidates))
    return (
        f"{prompt_text}\n\n"
        f"MANDATORY CANDIDATE TOP-UP PASS {attempt}:\n"
        f"- Only {len(candidates)} usable distinct candidates survived validation.\n"
        f"- Find at least {missing} additional visually strong candidates.\n"
        "- Do not overlap or repeat any existing candidate interval listed below.\n"
        "- Chinese subtitles remain a hard rejection. Do not weaken any visual-quality rule.\n"
        f"Existing source intervals: {json.dumps(existing, ensure_ascii=False)}"
    )


def normalize(raw: dict, material, duration: float, profile: dict) -> list[dict]:
    review = profile["gemma_review"]
    hard_filters = review.get("hard_filters") or {}
    allowed_events = set(review["events"])
    default_event = review["default_event"]
    output = []
    for index, item in enumerate(raw.get("candidates") or [], 1):
        if hard_filters.get("reject_chinese_subtitles", False):
            subtitle_flag = item.get("chinese_subtitles_present") is True
            risk_text = " ".join(str(value) for value in (item.get("risks") or [])).lower()
            subtitle_risk = any(
                marker in risk_text
                for marker in (
                    "chinese subtitle", "chinese caption", "中文字幕",
                    "简体字幕", "繁体字幕",
                )
            )
            if subtitle_flag or subtitle_risk:
                continue
        try:
            start = max(0.0, float(item["start"]))
            end = min(duration, float(item["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start or end - start < 0.4:
            continue
        peak = float(item.get("peak_time") or ((start + end) / 2))
        scores = {
            key: max(0.0, min(1.0, float(item.get(key) or 0.0)))
            for key in ("aesthetic", "payoff", "action_intensity", "subject_visibility")
        }
        output.append(
            {
                "candidate_id": f"{material.video_id}_gemma_{index:02d}",
                "video_id": material.video_id,
                "video_path": material.path,
                "event": (
                    str(item.get("event"))
                    if str(item.get("event")) in allowed_events
                    else default_event
                ),
                "source_window": {"start": round(start, 3), "end": round(end, 3)},
                "preferred_trim": {"start": round(start, 3), "end": round(end, 3)},
                "peak_time": round(max(start, min(end, peak)), 3),
                "description": str(item.get("description") or ""),
                "chinese_subtitles_present": bool(
                    item.get("chinese_subtitles_present", False)
                ),
                "scores": {
                    **scores,
                    "category_event_value": 1.0,
                    "sharpness": 0.7,
                    "composition": 0.7,
                    "context_independence": 0.85,
                },
                "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
                "risks": list(item.get("risks") or []),
                "penalties": {},
                "roles": ["visual_montage"],
            }
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--category", default="beauty")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-audio", action="store_true")
    parser.add_argument("--cache-only", action="store_true")
    args = parser.parse_args()
    if args.force and args.cache_only:
        parser.error("--force and --cache-only cannot be used together")

    load_dotenv(args.env_file)
    project_root = Path(__file__).resolve().parents[1]
    profile_path = args.profile or project_root / "profiles" / "categories" / f"{args.category}.yaml"
    profile = load_yaml(profile_path)
    if profile.get("category_id") != args.category:
        raise SystemExit(f"Profile category_id does not match --category: {profile_path}")
    review = profile.get("gemma_review")
    if not review:
        raise SystemExit(f"Profile has no gemma_review configuration: {profile_path}")
    prompt_path = args.prompt_file or project_root / review["prompt_path"]
    prompt_template = prompt_path.read_text(encoding="utf-8")
    selected = [
        item for item in load_manifest(args.manifest)
        if item.enabled and item.category == args.category
    ][: args.limit]
    if not selected:
        raise SystemExit("No matching materials")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = args.output_dir / "raw"
    proxy_dir = args.output_dir / "proxies"
    raw_dir.mkdir(exist_ok=True)
    proxy_dir.mkdir(exist_ok=True)
    audio_dir = args.output_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    combined = []
    audio_bgm_results = []
    failures = []
    routing_summary = {
        "marlin": 0,
        "gemma_only": 0,
        "marlin_fallback": 0,
        "cache_hit": 0,
        "cache_miss": 0,
    }
    history_config = ((profile.get("batch_generation") or {}).get("history") or {})
    registry_path = Path(
        history_config.get("registry_path", "data/catalog/candidate-registry.sqlite")
    )
    if not registry_path.is_absolute():
        registry_path = project_root / registry_path

    for position, material in enumerate(selected, 1):
        started = time.monotonic()
        try:
            source_path = Path(material.path)
            cache_identity = analysis_cache_identity(
                video=source_path,
                video_id=material.video_id,
                category=args.category,
                profile=profile,
                prompt_template=prompt_template,
                project_root=project_root,
            )
            cached = None
            if history_config.get("enabled", False) and not args.force:
                with CandidateRegistry(registry_path) as registry:
                    cached = registry.get_analysis_cache(cache_identity["cache_key"])
            if cached:
                cached_models = [
                    VisualCandidate.model_validate(item)
                    for item in cached.get("candidates") or []
                ]
                if history_config.get("enabled", False):
                    with CandidateRegistry(registry_path) as registry:
                        cached_models = registry.register(
                            cached_models,
                            args.category,
                            increment_analysis=False,
                        )
                candidates = [item.model_dump() for item in cached_models]
                audio_bgm = None
                if (profile.get("audio_bgm_analysis") or {}).get("enabled", False):
                    with CandidateRegistry(registry_path) as registry:
                        audio_bgm = analyze_video_bgm(
                            video=source_path,
                            video_id=material.video_id,
                            category=args.category,
                            source_fingerprint=cache_identity["source_fingerprint"],
                            profile=profile,
                            registry=registry,
                            output_dir=audio_dir,
                            force=args.force_audio,
                            cache_only=args.cache_only,
                        )
                    audio_bgm_results.append(audio_bgm)
                combined.extend(candidates)
                routing_summary["cache_hit"] += 1
                cached_payload = {
                    **cached,
                    "material": material.model_dump(),
                    "cache": {
                        "hit": True,
                        "key": cache_identity["cache_key"],
                        "forced": False,
                    },
                    "analysis_route": f"cache_hit:{cached.get('analysis_route', 'unknown')}",
                    "audio_bgm": audio_bgm,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
                write_json(raw_dir / f"{material.video_id}.json", cached_payload)
                print(
                    f"[{position}/{len(selected)}] {material.video_id}: "
                    f"{len(candidates)} candidates (cache_hit)",
                    flush=True,
                )
                continue
            routing_summary["cache_miss"] += 1
            if args.cache_only:
                raise RuntimeError("analysis cache miss while --cache-only is enabled")

            duration = probe_duration(source_path)
            proxy = proxy_dir / f"{material.video_id}.mp4"
            compress(source_path, proxy)
            marlin = None
            route = "gemma_only"
            if should_use_marlin(profile, duration):
                try:
                    marlin = call_marlin(
                        proxy, material, profile, project_root, duration
                    )
                    route = (
                        "marlin_segmented_then_gemma"
                        if marlin.get("segmented")
                        else "marlin_then_gemma"
                    )
                    routing_summary["marlin"] += 1
                except Exception as exc:
                    if not profile["marlin_recall"].get("fail_open", True):
                        raise
                    marlin = {"error": str(exc), "queries": []}
                    route = "marlin_failed_gemma_fallback"
                    routing_summary["marlin_fallback"] += 1
            else:
                routing_summary["gemma_only"] += 1
            prompt_text = render_prompt(
                prompt_template,
                profile,
                material.video_id,
                duration,
                (marlin or {}).get("queries"),
            )
            gemma_review = {"mode": "full_proxy", "window_count": 1}
            if marlin and not marlin.get("error"):
                analysis, envelope, gemma_review = call_gemma_on_marlin_windows(
                    proxy,
                    material,
                    profile,
                    marlin,
                    prompt_text,
                    duration,
                    args.timeout,
                )
                route = f"{route}_candidate_windows"
            elif should_use_marlin(profile, duration):
                raise RuntimeError(
                    f"Marlin recall failed; refusing full-video Gemma fallback: "
                    f"{(marlin or {}).get('error', 'unknown error')}"
                )
            else:
                review_config = profile.get("gemma_review") or {}
                analysis, envelope = call_gemma(
                    proxy,
                    prompt_text,
                    args.timeout,
                    max_attempts=int(review_config.get("api_max_attempts", 3)),
                    retry_delays=tuple(
                        float(value)
                        for value in review_config.get(
                            "api_retry_delays_seconds", [2.0, 5.0]
                        )
                    ),
                )
            candidates = deduplicate_candidates(
                normalize(analysis, material, duration, profile)
            )
            review_config = profile.get("gemma_review") or {}
            minimum_candidates = max(
                1,
                int(review_config.get(
                    "minimum_candidates_per_video",
                    (review_config.get("candidate_count") or {}).get("minimum", 1),
                )),
            )
            topup_attempts = max(
                0, int(review_config.get("candidate_topup_attempts", 2))
            )
            topups = []
            for topup_attempt in range(1, topup_attempts + 1):
                if len(candidates) >= minimum_candidates:
                    break
                topup_text = candidate_topup_prompt(
                    prompt_text,
                    candidates,
                    minimum_candidates,
                    topup_attempt,
                )
                if marlin and not marlin.get("error"):
                    extra_analysis, extra_envelope, extra_review = (
                        call_gemma_on_marlin_windows(
                            proxy,
                            material,
                            profile,
                            marlin,
                            topup_text,
                            duration,
                            args.timeout,
                            checkpoint_namespace=f"topup-{topup_attempt}",
                        )
                    )
                else:
                    extra_analysis, extra_envelope = call_gemma(
                        proxy,
                        topup_text,
                        args.timeout,
                        max_attempts=int(review_config.get("api_max_attempts", 3)),
                        retry_delays=tuple(
                            float(value)
                            for value in review_config.get(
                                "api_retry_delays_seconds", [2.0, 5.0]
                            )
                        ),
                    )
                    extra_review = {"mode": "full_proxy_topup"}
                analysis = merge_gemma_analyses(analysis, extra_analysis)
                candidates = deduplicate_candidates(
                    normalize(analysis, material, duration, profile)
                )
                topups.append({
                    "attempt": topup_attempt,
                    "candidate_count_after": len(candidates),
                    "usage": extra_envelope.get("usage"),
                    "review": extra_review,
                })
            gemma_review["minimum_candidates_per_video"] = minimum_candidates
            gemma_review["topups"] = topups
            gemma_review["minimum_satisfied"] = (
                len(candidates) >= minimum_candidates
            )
            gemma_review["candidate_shortfall"] = max(
                0, minimum_candidates - len(candidates)
            )
            if not candidates:
                raise RuntimeError(
                    f"no usable candidates after {topup_attempts} "
                    f"top-up pass(es)"
                )
            if history_config.get("enabled", False):
                with CandidateRegistry(registry_path) as registry:
                    registered = registry.register(
                        [VisualCandidate.model_validate(item) for item in candidates],
                        args.category,
                    )
                candidates = [item.model_dump() for item in registered]
            combined.extend(candidates)
            raw_payload = {
                "material": material.model_dump(),
                "duration_seconds": duration,
                "analysis": analysis,
                "candidates": candidates,
                "profile_path": str(profile_path),
                "prompt_path": str(prompt_path),
                "analysis_route": route,
                "marlin": marlin,
                "gemma_review": gemma_review,
                "usage": envelope.get("usage"),
                "cache": {
                    "hit": False,
                    "key": cache_identity["cache_key"],
                    "forced": bool(args.force),
                },
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            if (profile.get("audio_bgm_analysis") or {}).get("enabled", False):
                with CandidateRegistry(registry_path) as registry:
                    raw_payload["audio_bgm"] = analyze_video_bgm(
                        video=source_path,
                        video_id=material.video_id,
                        category=args.category,
                        source_fingerprint=cache_identity["source_fingerprint"],
                        profile=profile,
                        registry=registry,
                        output_dir=audio_dir,
                        force=args.force_audio,
                        cache_only=args.cache_only,
                    )
                audio_bgm_results.append(raw_payload["audio_bgm"])
            write_json(raw_dir / f"{material.video_id}.json", raw_payload)
            if history_config.get("enabled", False):
                with CandidateRegistry(registry_path) as registry:
                    registry.put_analysis_cache(
                        cache_key=cache_identity["cache_key"],
                        video_id=material.video_id,
                        video_path=material.path,
                        category=args.category,
                        source_fingerprint=cache_identity["source_fingerprint"],
                        configuration_fingerprint=cache_identity["configuration_fingerprint"],
                        model_id=cache_identity["model_id"],
                        payload=raw_payload,
                    )
            print(
                f"[{position}/{len(selected)}] {material.video_id}: "
                f"{len(candidates)} candidates ({route})",
                flush=True,
            )
        except Exception as exc:
            failures.append({"video_id": material.video_id, "path": material.path, "error": str(exc)})
            print(f"[{position}/{len(selected)}] {material.video_id}: FAILED {exc}", flush=True)

    contact_sheet = generate_contact_sheet(
        combined,
        args.output_dir / "contact-sheet.jpg",
        profile,
    )
    write_json(
        args.output_dir / "candidate-pool.json",
        {
            "schema_version": "1.0",
            "category": args.category,
            "video_count": len(selected),
            "candidate_count": len(combined),
            "candidates": combined,
            "failures": failures,
            "routing_summary": routing_summary,
            "contact_sheet": contact_sheet,
            "candidate_registry": {
                "enabled": bool(history_config.get("enabled", False)),
                "path": str(registry_path),
            },
            "bgm_analysis": {
                "enabled": bool(
                    (profile.get("audio_bgm_analysis") or {}).get("enabled", False)
                ),
                "analyzed_count": len(audio_bgm_results),
                "eligible_count": sum(
                    bool(item and item.get("eligible_as_bgm"))
                    for item in audio_bgm_results
                ),
                "items": audio_bgm_results,
            },
        },
    )
    print(
        json.dumps(
            {
                "videos": len(selected),
                "candidates": len(combined),
                "failures": len(failures),
                "routing": routing_summary,
                "contact_sheet": contact_sheet,
            },
            ensure_ascii=False,
        )
    )
    # Preserve successful work when one remote request fails. Per-video errors
    # remain in candidate-pool.json for reporting and retry; downstream stages
    # may continue whenever a usable candidate pool exists.
    return 0 if combined else 1


if __name__ == "__main__":
    raise SystemExit(main())
