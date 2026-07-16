"""Generate a browser preview page for a clip-plan JSON."""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_PLAN = Path("data/rednote_workspace/runs/20260703_162513/clip_plan_bgm_6a05a61b.json")
CLIP_PROXY_VERSION = 2


def _resolve_media_uri(path_value: str, base_dir: Path) -> str:
    if not path_value:
        return ""
    if path_value.startswith(("http://", "https://", "file://", "data:")):
        return path_value
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        candidates = [Path.cwd() / path, base_dir / path]
        path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    return path.resolve().as_uri()


def _enrich_plan(plan: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    base_dir = plan_path.resolve().parent
    enriched = json.loads(json.dumps(plan, ensure_ascii=False))

    bgm = enriched.get("bgm") or {}
    if not bgm.get("media_uri"):
        bgm["media_uri"] = _resolve_media_uri(str(bgm.get("audio_path") or ""), base_dir)
    enriched["bgm"] = bgm

    for clip in enriched.get("timeline") or []:
        source = clip.get("source") or {}
        if not source.get("media_uri"):
            source["media_uri"] = _resolve_media_uri(str(source.get("video_path") or ""), base_dir)
        clip["source"] = source

    assets = enriched.setdefault("assets", {})
    for asset_key in ("openpage", "category_screenshot", "tail_sticker", "logo_overlay"):
        if not isinstance(assets.get(asset_key), dict):
            continue
        item = assets[asset_key]
        if asset_key == "logo_overlay":
            if not item.get("media_uri") and item.get("image_path"):
                item["media_uri"] = _resolve_media_uri(str(item.get("image_path")), base_dir)
        else:
            if not item.get("media_uri") and item.get("video_path"):
                item["media_uri"] = _resolve_media_uri(str(item.get("video_path")), base_dir)
        assets[asset_key] = item
    enriched["assets"] = assets
    return enriched


def _run_ffmpeg(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _probe_video_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def _has_video_stream(path: Path) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return False
    return "video" in result.stdout


def _relative_media_uri(path: Path, base_dir: Path) -> str:
    return os.path.relpath(path.resolve(), base_dir.resolve()).replace(os.sep, "/")


def _make_clip_proxy(clip: dict[str, Any], output_path: Path) -> None:
    source = clip.get("source") or {}
    if str(source.get("clip_type") or "") == "fixed_cover":
        _make_cover_proxy(clip, output_path)
        return
    video_path = Path(str(source.get("video_path") or "")).expanduser()
    if not video_path.is_absolute():
        video_path = Path.cwd() / video_path
    source_in = max(0.0, float(source.get("source_in") or 0.0))
    duration = max(0.05, float(clip.get("duration") or 0.0))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        "scale='if(gt(a,9/16),-2,1080)':'if(gt(a,9/16),1920,-2)',"
        "crop=1080:1920,setsar=1"
    )
    timed_vf = (
        f"{vf},fps=30,"
        f"tpad=stop_mode=clone:stop_duration={duration:.3f},"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS"
    )
    ext = video_path.suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        command = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(video_path),
            "-t",
            f"{duration:.3f}",
            "-an",
            "-vf",
            vf,
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    else:
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{source_in:.3f}",
            "-i",
            str(video_path),
            "-an",
            "-vf",
            timed_vf,
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    _run_ffmpeg(command)


def _sample_frame_theme_color(video_path: Path, timestamp: float) -> str:
    vf = (
        "scale='if(gt(a,9/16),-2,1080)':'if(gt(a,9/16),1920,-2)',"
        "crop=1080:1920,scale=1:1,format=rgb24"
    )
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{max(0.0, timestamp):.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        return "F6E76B"
    if len(result.stdout) < 3:
        return "F6E76B"
    r, g, b = result.stdout[:3]
    hue, saturation, value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    saturation = min(0.78, max(0.42, saturation + 0.25))
    value = min(0.98, max(0.72, value + 0.28))
    rr, gg, bb = colorsys.hsv_to_rgb(hue, saturation, value)
    return f"{round(rr * 255):02X}{round(gg * 255):02X}{round(bb * 255):02X}"


def _split_cover_title(title: str) -> str:
    raw_text = str(title or "").strip().replace("\\n", "\n").replace("|", "\n")
    paragraphs = [" ".join(part.split()) for part in raw_text.splitlines()]
    text = "\n".join(part for part in paragraphs if part)
    if not text:
        return "Highlight"
    if "\n" in text:
        return text
    return text


def _escape_cover_drawtext_value(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("%", "\\%")
        .replace("\n", " ")
    )


def _make_cover_proxy(clip: dict[str, Any], output_path: Path) -> None:
    source = clip.get("source") or {}
    cover = clip.get("cover") or {}
    video_path = Path(str(source.get("video_path") or "")).expanduser()
    if not video_path.is_absolute():
        video_path = Path.cwd() / video_path
    frame_at = max(0.0, float(source.get("source_in") or 0.0))
    source_duration = _probe_video_duration(video_path)
    if source_duration is not None and source_duration > 0:
        frame_at = min(frame_at, max(0.0, source_duration - 0.5))
    duration = max(0.05, float(clip.get("duration") or 0.0))
    title = _split_cover_title(str(cover.get("title") or "Highlight"))
    cover_font_value = str(cover.get("font_path") or "")
    cover_font_path = _resolve_local_path(cover_font_value, output_path.parent) if cover_font_value else None
    font_path = cover_font_path if cover_font_path and cover_font_path.exists() else _drawtext_font_path()
    font_part = f"fontfile='{_escape_drawtext_value(str(font_path))}':" if font_path else ""
    title_lines = [line for line in title.splitlines() if line.strip()] or ["Highlight"]
    accent = _sample_frame_theme_color(video_path, frame_at)
    escaped_accent = f"0x{accent}"
    title_size = 136
    line_gap = 12
    line_count = len(title_lines)
    title_block_height = f"({line_count}*{title_size}+{max(0, line_count - 1)}*{line_gap})"
    vf_parts = [
        "scale='if(gt(a,9/16),-2,1080)':'if(gt(a,9/16),1920,-2)'",
        "crop=1080:1920,setsar=1",
        "trim=end_frame=1",
        f"tpad=stop_mode=clone:stop_duration={duration:.3f}",
        f"trim=duration={duration:.3f}",
        "eq=contrast=1.06:saturation=1.08:brightness=-0.015",
    ]
    for line_index, line in enumerate(title_lines):
        text = _escape_cover_drawtext_value(line)
        y_expr = f"h*0.75-{title_block_height}/2+{line_index}*({title_size}+{line_gap})"
        vf_parts.extend(
            [
                (
                    "drawtext="
                    f"{font_part}text='{text}':x=(w-tw)/2+8:y=({y_expr})+10:"
                    f"fontsize={title_size}:fontcolor=black@0.62:"
                    "borderw=9:bordercolor=black@0.48"
                ),
                (
                    "drawtext="
                    f"{font_part}text='{text}':x=(w-tw)/2:y={y_expr}:"
                    f"fontsize={title_size}:fontcolor={escaped_accent}:"
                    "borderw=8:bordercolor=black@0.72:shadowx=0:shadowy=7:shadowcolor=white@0.2"
                ),
            ]
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def render_cover(at_seconds: float) -> None:
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{max(0.0, at_seconds):.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{duration:.3f}",
            "-an",
            "-vf",
            ",".join(vf_parts),
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        _run_ffmpeg(command)

    retry_points = [frame_at]
    if source_duration is not None and source_duration > 0:
        retry_points.extend([max(0.0, source_duration - 1.0), 0.0])
    else:
        retry_points.extend([max(0.0, frame_at - 1.0), 0.0])
    tried: set[float] = set()
    for retry_at in retry_points:
        rounded_retry_at = round(retry_at, 3)
        if rounded_retry_at in tried:
            continue
        tried.add(rounded_retry_at)
        render_cover(rounded_retry_at)
        if _has_video_stream(output_path):
            return
    raise RuntimeError(f"封面预览代理生成失败，没有视频流: {output_path}")


def _make_bgm_proxy(plan: dict[str, Any], output_path: Path) -> None:
    bgm = plan.get("bgm") or {}
    audio_path = Path(str(bgm.get("audio_path") or "")).expanduser()
    if not audio_path.is_absolute():
        audio_path = Path.cwd() / audio_path
    duration = float(plan.get("output", {}).get("duration_seconds") or bgm.get("duration_seconds") or 0.0)
    command = [
        "ffmpeg",
        "-y",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(audio_path),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(command)


def build_proxy_plan(plan: dict[str, Any], plan_path: Path, proxy_dir: Path, overwrite: bool = False) -> dict[str, Any]:
    proxy_dir.mkdir(parents=True, exist_ok=True)
    proxy_plan = json.loads(json.dumps(plan, ensure_ascii=False))
    manifest_path = proxy_dir / "manifest.json"
    try:
        proxy_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    except json.JSONDecodeError:
        proxy_manifest = {}

    bgm_proxy = proxy_dir / "bgm.m4a"
    bgm_signature = _bgm_proxy_signature(proxy_plan)
    bgm_manifest = proxy_manifest.get("bgm") if isinstance(proxy_manifest.get("bgm"), dict) else {}
    if overwrite or not bgm_proxy.exists() or bgm_manifest.get("signature") != bgm_signature:
        _make_bgm_proxy(proxy_plan, bgm_proxy)
    proxy_manifest["bgm"] = {"signature": bgm_signature, "path": str(bgm_proxy)}
    proxy_plan.setdefault("bgm", {})["media_uri"] = _relative_media_uri(bgm_proxy, plan_path.resolve().parent)
    proxy_plan["preview_proxy_dir"] = str(proxy_dir)

    clip_manifest: dict[str, Any] = {}
    old_clip_manifest = proxy_manifest.get("clips") if isinstance(proxy_manifest.get("clips"), dict) else {}
    for index, clip in enumerate(proxy_plan.get("timeline") or [], start=1):
        clip_id = str(clip.get("clip_id") or f"clip_{index:03d}")
        clip_proxy = proxy_dir / f"{clip_id}.mp4"
        signature = _clip_proxy_signature(clip)
        old_entry = old_clip_manifest.get(clip_id) if isinstance(old_clip_manifest.get(clip_id), dict) else {}
        if overwrite or not clip_proxy.exists() or old_entry.get("signature") != signature:
            _make_clip_proxy(clip, clip_proxy)
        clip_manifest[clip_id] = {
            "signature": signature,
            "path": str(clip_proxy),
            "source": {
                "video_path": (clip.get("source") or {}).get("video_path", ""),
                "source_in": (clip.get("source") or {}).get("source_in", 0.0),
                "duration": clip.get("duration", 0.0),
            },
        }
        source = clip.setdefault("source", {})
        source["media_uri"] = _relative_media_uri(clip_proxy, plan_path.resolve().parent)
        source["preview_proxy_path"] = str(clip_proxy)
        source["source_in_original"] = source.get("source_in", 0.0)
        source["source_out_original"] = source.get("source_out", 0.0)
        source["source_in"] = 0.0
        source["source_out"] = float(clip.get("duration") or 0.0)
    proxy_manifest["clips"] = clip_manifest
    manifest_path.write_text(json.dumps(proxy_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _enrich_plan(proxy_plan, plan_path)


def _stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clip_proxy_signature(clip: dict[str, Any]) -> str:
    source = clip.get("source") or {}
    return _stable_digest(
        {
            "proxy_version": CLIP_PROXY_VERSION,
            "video_path": str(source.get("video_path") or ""),
            "source_in": round(float(source.get("source_in") or 0.0), 3),
            "duration": round(float(clip.get("duration") or 0.0), 3),
            "timeline_in": round(float(clip.get("timeline_in") or 0.0), 3),
            "cover": clip.get("cover") or {},
        }
    )


def _bgm_proxy_signature(plan: dict[str, Any]) -> str:
    bgm = plan.get("bgm") or {}
    return _stable_digest(
        {
            "audio_path": str(bgm.get("audio_path") or ""),
            "duration": round(float(plan.get("output", {}).get("duration_seconds") or bgm.get("duration_seconds") or 0.0), 3),
        }
    )


def _author_color_from_logo(plan: dict[str, Any]) -> str:
    logo = (plan.get("assets") or {}).get("logo_overlay") or {}
    name_parts = [
        Path(str(logo.get("image_path") or "")).name,
        Path(str(logo.get("media_uri") or "")).name,
    ]
    normalized = " ".join(name_parts).lower()
    if "黑字" in normalized or "black" in normalized:
        return "black"
    if "白字" in normalized or "white" in normalized:
        return "white"
    return "white"


def _output_resolution(plan: dict[str, Any]) -> tuple[int, int]:
    value = str((plan.get("output") or {}).get("resolution") or "")
    if "x" in value.lower():
        width_text, height_text = value.lower().split("x", 1)
        try:
            width = int(width_text.strip())
            height = int(height_text.strip())
            if width > 0 and height > 0:
                return width, height
        except ValueError:
            pass
    return 1080, 1920


def build_preview_html(plan: dict[str, Any], plan_path: Path) -> str:
    plan_json = json.dumps(plan, ensure_ascii=False)
    title = escape(str(plan.get("plan_id") or plan_path.stem))
    duration = plan.get("output", {}).get("duration_seconds", "")
    clip_count = len(plan.get("timeline") or [])
    bgm = plan.get("bgm") or {}
    bgm_path = escape(str(bgm.get("audio_path", "")))
    bgm_bpm = escape(str(bgm.get("bpm", "")))
    max_clip_count = max(clip_count, 1)
    duration_text = escape(str(duration))
    duration_or_zero = escape(str(duration or 0))
    author_color = _author_color_from_logo(plan)
    author_css_color = "rgba(18, 18, 18, 0.96)" if author_color == "black" else "rgba(255, 255, 255, 0.96)"
    author_overlay = (plan.get("assets") or {}).get("author_id_overlay") or {}
    author_font_percent = float(author_overlay.get("font_size_percent") or 4.5)
    author_margin_percent = float(author_overlay.get("margin_x_percent") or 4.45)
    author_top_percent = float(author_overlay.get("top_percent") or 5.2)

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} Preview</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #111313;
      --panel: #1a1d1d;
      --panel-2: #202525;
      --line: #303737;
      --text: #eef4f1;
      --muted: #9eaaa5;
      --accent: #58c4b8;
      --accent-2: #f1c46b;
      --danger: #ff7a7a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    button, input {{ font: inherit; }}
    .app {{
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(360px, 1fr);
      min-height: 100vh;
    }}
    .sidebar {{
      border-right: 1px solid var(--line);
      background: var(--panel);
      display: flex;
      flex-direction: column;
      min-width: 0;
    }}
    .header {{
      padding: 18px 18px 14px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 18px;
      line-height: 1.25;
      font-weight: 760;
    }}
    .meta {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .metric {{
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      min-width: 0;
    }}
    .metric b {{
      display: block;
      font-size: 13px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      margin-top: 2px;
    }}
    .asset-summary {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 12px;
    }}
    .asset-item {{
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: rgba(255, 255, 255, 0.03);
    }}
    .asset-item strong {{
      display: block;
      font-size: 12px;
      line-height: 1.2;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .asset-item span {{
      display: block;
      margin-top: 3px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.25;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .clip-list {{
      overflow: auto;
      padding: 8px;
      flex: 1;
    }}
    .clip-row {{
      width: 100%;
      display: grid;
      grid-template-columns: 48px 1fr 54px;
      gap: 10px;
      align-items: center;
      text-align: left;
      color: var(--text);
      background: transparent;
      border: 1px solid transparent;
      border-radius: 8px;
      padding: 9px 8px;
      cursor: pointer;
    }}
    .clip-row:hover {{ background: #232929; }}
    .clip-row.active {{
      border-color: color-mix(in srgb, var(--accent) 70%, transparent);
      background: #213130;
    }}
    .clip-row strong {{
      display: block;
      font-size: 13px;
      line-height: 1.25;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .clip-row small {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.3;
      margin-top: 3px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .time-pill {{
      color: var(--accent-2);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }}
    .stage {{
      min-width: 0;
      display: grid;
      grid-template-rows: 1fr auto;
      background: #0d0f0f;
    }}
    .viewer {{
      min-height: 0;
      display: grid;
      place-items: center;
      padding: 22px;
    }}
    .phone {{
      height: min(78vh, 860px);
      aspect-ratio: 9 / 16;
      background: #050606;
      border: 1px solid #2a3030;
      border-radius: 8px;
      overflow: hidden;
      position: relative;
      container-type: inline-size;
      box-shadow: 0 24px 70px rgba(0, 0, 0, 0.35);
    }}
    video {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      background: #050606;
    }}
    .overlay {{
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      padding: 14px;
      background: linear-gradient(to top, rgba(0,0,0,.72), rgba(0,0,0,0));
      pointer-events: none;
    }}
    .overlay b {{
      display: block;
      font-size: 13px;
      line-height: 1.25;
      text-shadow: 0 1px 2px rgba(0,0,0,.5);
    }}
    .overlay span {{
      display: block;
      margin-top: 4px;
      color: rgba(255,255,255,.78);
      font-size: 11px;
      line-height: 1.35;
    }}
    .logo-overlay {{
      position: absolute;
      top: 18px;
      left: 18px;
      width: 100%;
      max-width: none;
      max-height: none;
      z-index: 2;
      pointer-events: none;
      display: none;
    }}
    .logo-overlay.full-canvas {{
      inset: 0;
      width: 100%;
      height: 100%;
      max-width: none;
      max-height: none;
    }}
    .logo-overlay.top-left {{ top: 18px; left: 18px; right: auto; bottom: auto; }}
    .logo-overlay.top-right {{ top: 18px; right: 18px; left: auto; bottom: auto; }}
    .logo-overlay.bottom-left {{ bottom: 18px; left: 18px; top: auto; right: auto; }}
    .logo-overlay.bottom-right {{ bottom: 18px; right: 18px; top: auto; left: auto; }}
    .logo-overlay img {{
      width: 100%;
      height: auto;
      object-fit: contain;
      display: block;
      filter: drop-shadow(0 3px 10px rgba(0, 0, 0, 0.45));
    }}
    .logo-overlay.full-canvas img {{
      width: 100%;
      height: 100%;
      object-fit: fill;
    }}
    .author-overlay {{
      position: absolute;
      top: {author_top_percent:.2f}%;
      right: {author_margin_percent:.2f}%;
      z-index: 3;
      display: none;
      max-width: 42%;
      color: {author_css_color};
      font-size: clamp(16px, {author_font_percent:.2f}cqw, 48px);
      line-height: 1.2;
      font-weight: 760;
      text-align: right;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      pointer-events: none;
    }}
    .controls {{
      border-top: 1px solid var(--line);
      background: var(--panel);
      padding: 14px 18px 18px;
    }}
    .transport {{
      display: grid;
      grid-template-columns: auto auto auto minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      margin-bottom: 12px;
    }}
    .icon-btn {{
      width: 40px;
      height: 40px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--text);
      display: inline-grid;
      place-items: center;
      cursor: pointer;
    }}
    .icon-btn:hover {{ border-color: var(--accent); }}
    .time-readout {{
      font-variant-numeric: tabular-nums;
      color: var(--muted);
      font-size: 13px;
      display: inline-flex;
      gap: 4px;
      white-space: nowrap;
    }}
    .scrubber {{
      width: 100%;
      accent-color: var(--accent);
    }}
    .bars {{
      display: grid;
      grid-template-columns: repeat({max(clip_count, 1)}, minmax(8px, 1fr));
      gap: 2px;
      height: 18px;
      margin-top: 8px;
    }}
    .bar {{
      background: #313838;
      border-radius: 2px;
      position: relative;
      overflow: hidden;
    }}
    .bar.active {{ background: var(--accent); }}
    .status {{
      color: var(--muted);
      font-size: 12px;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .error {{ color: var(--danger); }}
    .export-btn {{
      padding: 8px 12px;
      border-radius: 8px;
      border: 1px solid var(--accent);
      background: rgba(88, 196, 184, 0.1);
      color: var(--accent);
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
      transition: all 0.2s;
    }}
    .export-btn:hover {{
      background: rgba(88, 196, 184, 0.2);
      border-color: var(--accent);
    }}
    .export-btn:disabled {{
      opacity: 0.5;
      cursor: not-allowed;
    }}
    .modal {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.6);
      backdrop-filter: blur(2px);
      z-index: 1000;
      place-items: center;
    }}
    .modal.active {{
      display: grid;
    }}
    .modal-content {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 24px;
      max-width: 480px;
      width: 90%;
      max-height: 80vh;
      overflow: auto;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    }}
    .modal-header {{
      font-size: 16px;
      font-weight: 700;
      margin-bottom: 16px;
    }}
    .modal-body {{
      margin-bottom: 20px;
      color: var(--text);
    }}
    .export-form {{
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .form-group {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .form-group label {{
      font-size: 12px;
      font-weight: 600;
      color: var(--muted);
    }}
    .form-group input {{
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      font-size: 12px;
    }}
    .export-progress {{
      display: none;
      flex-direction: column;
      gap: 12px;
    }}
    .export-progress.active {{
      display: flex;
    }}
    .progress-bar {{
      width: 100%;
      height: 6px;
      background: var(--panel-2);
      border-radius: 3px;
      overflow: hidden;
    }}
    .progress-bar-fill {{
      height: 100%;
      background: var(--accent);
      width: 0%;
      transition: width 0.3s;
    }}
    .export-status {{
      font-size: 12px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .export-status.success {{
      color: var(--accent);
    }}
    .export-status.error {{
      color: var(--danger);
    }}
    .export-result {{
      display: none;
      margin-top: 12px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.03);
      font-size: 12px;
      line-height: 1.5;
    }}
    .export-result.active {{
      display: block;
    }}
    .export-result a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 700;
      margin-right: 12px;
    }}
    .export-result code {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .modal-footer {{
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 10px;
    }}
    .modal-btn {{
      padding: 8px 14px;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--text);
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
      transition: all 0.2s;
    }}
    .modal-btn:hover {{
      border-color: var(--accent);
      background: rgba(88, 196, 184, 0.05);
    }}
    .modal-btn.primary {{
      background: rgba(88, 196, 184, 0.2);
      border-color: var(--accent);
      color: var(--accent);
    }}
    .modal-btn.primary:hover {{
      background: rgba(88, 196, 184, 0.3);
    }}
    @media (max-width: 900px) {{
      .app {{ grid-template-columns: 1fr; }}
      .sidebar {{ border-right: 0; border-bottom: 1px solid var(--line); max-height: 42vh; }}
      .viewer {{ padding: 12px; }}
      .phone {{ height: min(54vh, 720px); }}
    }}
  </style>
</head>
<body>
  <main class="app">
    <aside class="sidebar">
      <section class="header">
        <h1>{title}</h1>
        <div class="meta">
          <div class="metric"><b>{duration}s</b><span>时长</span></div>
          <div class="metric"><b>{clip_count}</b><span>镜头</span></div>
          <div class="metric"><b>{bgm_bpm}</b><span>BPM</span></div>
        </div>
        <div class="asset-summary" id="assetSummary"></div>
      </section>
      <section class="clip-list" id="clipList"></section>
    </aside>
    <section class="stage">
      <div class="viewer">
        <div class="phone">
          <video id="previewVideo" playsinline muted preload="auto"></video>
          <div class="logo-overlay" id="logoOverlay" style="display:none;">
            <img id="logoOverlayImg" src="" alt="logo overlay">
          </div>
          <div class="author-overlay" id="authorOverlay"></div>
          <div class="overlay">
            <b id="clipTitle">等待播放</b>
            <span id="clipDetail">点击播放按钮开始预览</span>
          </div>
        </div>
      </div>
      <section class="controls">
        <div class="transport">
          <button class="icon-btn" id="playBtn" title="播放/暂停" aria-label="播放或暂停">▶</button>
          <button class="icon-btn" id="restartBtn" title="回到开头" aria-label="回到开头">↺</button>
          <div class="time-readout"><span id="currentTime">00:00.000</span><span>/</span><span id="totalTime">{duration_or_zero}</span></div>
          <div class="status" id="status">BGM: {bgm_path}</div>
          <button class="export-btn" id="exportBtn" title="导出为MP4视频">🎬 导出视频</button>
        </div>
        <input class="scrubber" id="scrubber" type="range" min="0" max="{duration_or_zero}" value="0" step="0.001" aria-label="时间线">
        <div class="bars" id="bars"></div>
        <audio id="bgm" preload="auto"></audio>
      </section>
    </section>
  </main>

  <!-- Export Modal -->
  <div class="modal" id="exportModal">
    <div class="modal-content">
      <div class="modal-header">导出视频</div>
      <div class="modal-body">
        <div class="export-form" id="exportForm">
          <div class="form-group">
            <label for="exportOutput">输出文件名:</label>
            <input type="text" id="exportOutput" placeholder="output.mp4" value="output.mp4">
          </div>
        </div>
        <div class="export-progress" id="exportProgress">
          <div class="progress-bar">
            <div class="progress-bar-fill" id="progressBarFill"></div>
          </div>
          <div class="export-status" id="exportStatus">准备导出...</div>
        </div>
        <div class="export-result" id="exportResult"></div>
      </div>
      <div class="modal-footer">
        <div></div>
        <button class="modal-btn" id="exportCancelBtn">取消</button>
        <button class="modal-btn primary" id="exportConfirmBtn" type="button" onclick="startExport(event)">导出</button>
      </div>
    </div>
  </div>

  <script>
    const plan = {plan_json};
    const timeline = plan.timeline || [];
    const totalDuration = Number(plan.output?.duration_seconds || 0);
    const video = document.getElementById("previewVideo");
    const bgm = document.getElementById("bgm");
    const playBtn = document.getElementById("playBtn");
    const restartBtn = document.getElementById("restartBtn");
    const scrubber = document.getElementById("scrubber");
    const currentTimeLabel = document.getElementById("currentTime");
    const totalTimeLabel = document.getElementById("totalTime");
    const statusEl = document.getElementById("status");
    const clipTitle = document.getElementById("clipTitle");
    const clipDetail = document.getElementById("clipDetail");
    const clipList = document.getElementById("clipList");
    const bars = document.getElementById("bars");
    const assetSummary = document.getElementById("assetSummary");
    const logoOverlay = document.getElementById("logoOverlay");
    const logoOverlayImg = document.getElementById("logoOverlayImg");
    const authorOverlay = document.getElementById("authorOverlay");
    const exportBtn = document.getElementById("exportBtn");
    const exportModal = document.getElementById("exportModal");
    const exportForm = document.getElementById("exportForm");
    const exportProgress = document.getElementById("exportProgress");
    const exportStatus = document.getElementById("exportStatus");
    const exportResult = document.getElementById("exportResult");
    const progressBarFill = document.getElementById("progressBarFill");
    const exportOutput = document.getElementById("exportOutput");
    const exportConfirmBtn = document.getElementById("exportConfirmBtn");
    const exportCancelBtn = document.getElementById("exportCancelBtn");
    const canUseServerExport = location.protocol === "http:" || location.protocol === "https:";

    let activeIndex = -1;
    let playing = false;
    let rafId = 0;
    let exportAbortController = null;

    bgm.src = plan.bgm?.media_uri || "";
    if (totalTimeLabel) totalTimeLabel.textContent = formatTime(totalDuration);

    function renderAssetSummary() {{
      const assets = plan.assets || {{}};
      const rows = [];
      const addRow = (label, enabled, value) => {{
        rows.push(`
          <div class="asset-item">
            <strong>${{label}}</strong>
            <span>${{enabled ? value : "未配置"}}</span>
          </div>
        `);
      }};
      addRow("Openpage", Boolean(assets.openpage?.enabled), assets.openpage?.enabled ? `${{assets.openpage.duration_seconds}}s` : "");
      addRow("产品截图", Boolean(assets.category_screenshot?.enabled), assets.category_screenshot?.enabled ? `${{assets.category_screenshot.duration_seconds}}s` : "");
      addRow("Logo", Boolean(assets.logo_overlay?.enabled), assets.logo_overlay?.enabled ? (assets.logo_overlay.render_mode || assets.logo_overlay.position) : "");
      addRow("尾贴", Boolean(assets.tail_sticker?.enabled), assets.tail_sticker?.enabled ? `${{assets.tail_sticker.duration_seconds}}s` : "");
      assetSummary.innerHTML = rows.join("");
    }}

    function renderLogoOverlay(clip = null) {{
      const logo = plan.assets?.logo_overlay;
      const clipType = clip?.source?.clip_type || "";
      const hiddenTypes = Array.isArray(logo?.hide_on_clip_types) ? logo.hide_on_clip_types : [];
      const shouldHideForClip = hiddenTypes.includes(clipType);
      if (logo?.enabled && logo.media_uri) {{
        if (shouldHideForClip) {{
          logoOverlay.style.display = "none";
          return;
        }}
        const fullCanvas = logo.render_mode === "full_canvas";
        logoOverlayImg.src = logo.media_uri;
        logoOverlayImg.alt = `Logo: ${{logo.position || "logo"}}`;
        logoOverlay.classList.toggle("full-canvas", fullCanvas);
        logoOverlay.classList.toggle("top-left", !fullCanvas && logo.position !== "top_right" && logo.position !== "bottom_left" && logo.position !== "bottom_right");
        logoOverlay.classList.toggle("top-right", !fullCanvas && logo.position === "top_right");
        logoOverlay.classList.toggle("bottom-left", !fullCanvas && logo.position === "bottom_left");
        logoOverlay.classList.toggle("bottom-right", !fullCanvas && logo.position === "bottom_right");
        logoOverlay.style.display = "block";
      }} else {{
        logoOverlay.style.display = "none";
      }}
    }}

    function renderAuthorOverlay(clip = null) {{
      const author = String(clip?.source?.author || "").trim();
      if (author) {{
        authorOverlay.textContent = author;
        authorOverlay.style.display = "block";
      }} else {{
        authorOverlay.textContent = "";
        authorOverlay.style.display = "none";
      }}
    }}

    renderAssetSummary();

    timeline.forEach((clip, index) => {{
      const row = document.createElement("button");
      row.className = "clip-row";
      row.type = "button";
      row.dataset.index = String(index);
      row.innerHTML = `
        <span class="time-pill">${{formatTime(clip.timeline_in).slice(3)}}</span>
        <span><strong>${{escapeHtml(clip.source?.author || clip.clip_id || "clip")}}</strong><small>${{escapeHtml((clip.source?.tags || []).slice(0, 3).join(" / "))}}</small></span>
        <span class="time-pill">${{Number(clip.duration || 0).toFixed(2)}}s</span>
      `;
      row.addEventListener("click", () => seekTo(Number(clip.timeline_in || 0), true));
      clipList.appendChild(row);

      const bar = document.createElement("div");
      bar.className = "bar";
      bar.title = `${{clip.clip_id}} ${{formatTime(clip.timeline_in)}}`;
      bars.appendChild(bar);
    }});

    setClipForTime(0);
    updateUi(0);

    if (!canUseServerExport) {{
      exportBtn.disabled = true;
      exportBtn.title = "导出视频需要用 serve-clip-plan 启动本地预览服务";
      exportBtn.textContent = "导出需服务模式";
    }}

    // Export modal handlers
    exportBtn.addEventListener("click", () => {{
      if (!canUseServerExport) return;
      exportForm.style.display = "block";
      exportProgress.classList.remove("active");
      exportResult.classList.remove("active");
      exportResult.innerHTML = "";
      exportModal.classList.add("active");
      exportConfirmBtn.disabled = false;
      exportConfirmBtn.textContent = "导出";
    }});

    exportCancelBtn.addEventListener("click", () => {{
      if (exportAbortController) {{
        exportAbortController.abort();
        exportAbortController = null;
      }}
      exportModal.classList.remove("active");
      exportForm.style.display = "block";
      exportProgress.classList.remove("active");
    }});

    exportConfirmBtn.addEventListener("click", startExport);
    
    exportModal.addEventListener("click", (e) => {{
      if (e.target === exportModal) {{
        exportCancelBtn.click();
      }}
    }});

    async function startExport(event) {{
      if (event) event.preventDefault();
      if (exportConfirmBtn.disabled) return;
      const outputName = exportOutput.value.trim() || "output.mp4";
      if (!outputName.endsWith(".mp4")) {{
        setExportStatus("文件名必须以 .mp4 结尾", "error");
        return;
      }}

      exportForm.style.display = "none";
      exportProgress.classList.add("active");
      exportResult.classList.remove("active");
      exportResult.innerHTML = "";
      exportConfirmBtn.disabled = true;
      exportConfirmBtn.textContent = "导出中...";
      progressBarFill.style.width = "0%";
      setExportStatus("已点击导出，正在请求本地服务...");

      exportAbortController = new AbortController();

      try {{
        const response = await fetch("/api/export", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ output: outputName }}),
          signal: exportAbortController.signal,
        }});

        if (!response.ok) {{
          let error = {{}};
          try {{
            error = await response.json();
          }} catch (_error) {{
            error = {{ message: await response.text() }};
          }}
          throw new Error(error.message || `HTTP ${{response.status}}`);
        }}

        // Server-Sent Events 接收进度
        if (response.headers.get("content-type")?.includes("text/event-stream")) {{
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";

          while (true) {{
            const {{ done, value }} = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, {{ stream: true }});
            const lines = buffer.split("\\n");
            buffer = lines.pop() || "";

            for (const line of lines) {{
              if (line.startsWith("data: ")) {{
                try {{
                  const event = JSON.parse(line.slice(6));
                  if (event.progress !== undefined) {{
                    progressBarFill.style.width = event.progress + "%";
                  }}
                  if (event.message) {{
                    setExportStatus(event.message, event.status || "info");
                  }}
                  if (event.status === "success") {{
                    exportConfirmBtn.textContent = "导出";
                    progressBarFill.style.width = "100%";
                    showExportResult(event);
                  }}
                  if (event.status === "error") {{
                    exportConfirmBtn.disabled = false;
                    exportConfirmBtn.textContent = "导出";
                  }}
                }} catch (e) {{
                  console.error("Failed to parse event:", e);
                }}
              }}
            }}
          }}
        }} else {{
          const data = await response.json();
          if (data.success) {{
            setExportStatus("导出完成！文件已保存到: " + data.path, "success");
            progressBarFill.style.width = "100%";
            exportConfirmBtn.textContent = "导出";
            showExportResult(data);
          }} else {{
            throw new Error(data.message || "导出失败");
          }}
        }}
      }} catch (error) {{
        if (error.name === "AbortError") {{
          setExportStatus("导出已取消", "error");
        }} else {{
          setExportStatus("导出失败: " + error.message, "error");
        }}
        exportForm.style.display = "block";
        exportProgress.classList.remove("active");
        exportConfirmBtn.disabled = false;
        exportConfirmBtn.textContent = "导出";
      }} finally {{
        exportAbortController = null;
        exportConfirmBtn.disabled = false;
        if (exportConfirmBtn.textContent !== "导出") exportConfirmBtn.textContent = "导出";
      }}
    }}

    function setExportStatus(message, status = "info") {{
      exportStatus.textContent = message;
      exportStatus.className = "export-status";
      if (status !== "info") {{
        exportStatus.classList.add(status);
      }}
    }}

    function showExportResult(result) {{
      const url = result.url || "";
      const downloadUrl = result.download_url || url;
      const path = result.path || "";
      if (downloadUrl) {{
        const link = document.createElement("a");
        link.href = downloadUrl;
        link.download = "";
        document.body.appendChild(link);
        link.click();
        link.remove();
      }}
      exportResult.innerHTML = `
        <div>已导出，并已触发浏览器下载。</div>
        <div>
          ${{url ? `<a href="${{escapeHtml(url)}}" target="_blank" rel="noopener">打开视频</a>` : ""}}
          ${{downloadUrl ? `<a href="${{escapeHtml(downloadUrl)}}">下载视频</a>` : ""}}
        </div>
        ${{path ? `<code>${{escapeHtml(path)}}</code>` : ""}}
      `;
      exportResult.classList.add("active");
      exportForm.style.display = "block";
      exportProgress.classList.remove("active");
    }}

    playBtn.addEventListener("click", async () => {{
      if (playing) {{
        pause();
      }} else {{
        await play();
      }}
    }});
    restartBtn.addEventListener("click", () => seekTo(0, playing));
    scrubber.addEventListener("input", () => seekTo(Number(scrubber.value || 0), playing));
    video.addEventListener("error", () => setStatus(`视频加载失败: ${{timeline[activeIndex]?.source?.media_uri || timeline[activeIndex]?.source?.video_path || ""}}`, true));
    bgm.addEventListener("error", () => setStatus(`BGM加载失败: ${{plan.bgm?.audio_path || ""}}`, true));

    async function play() {{
      playing = true;
      playBtn.textContent = "Ⅱ";
      if (bgm.currentTime >= totalDuration) seekTo(0, false);
      try {{
        await bgm.play();
        await video.play();
        tick();
      }} catch (error) {{
        playing = false;
        playBtn.textContent = "▶";
        setStatus(`浏览器阻止了自动播放，请再次点击播放。${{error?.message || ""}}`, true);
      }}
    }}

    function pause() {{
      playing = false;
      playBtn.textContent = "▶";
      video.pause();
      bgm.pause();
      cancelAnimationFrame(rafId);
    }}

    function tick() {{
      const t = Math.min(Number(bgm.currentTime || 0), totalDuration);
      setClipForTime(t);
      updateUi(t);
      if (t >= totalDuration - 0.01) {{
        pause();
        seekTo(totalDuration, false);
        return;
      }}
      rafId = requestAnimationFrame(tick);
    }}

    function seekTo(time, resume) {{
      const t = clamp(time, 0, totalDuration);
      bgm.currentTime = t;
      setClipForTime(t, true);
      updateUi(t);
      if (resume) play();
    }}

    function setClipForTime(time, force = false) {{
      const index = findClipIndex(time);
      if (index === activeIndex && !force) {{
        const clip = timeline[index];
        if (clip && Math.abs(video.currentTime - sourceTimeFor(clip, time)) > 0.18) {{
          video.currentTime = sourceTimeFor(clip, time);
        }}
        return;
      }}
      activeIndex = index;
      const clip = timeline[index];
      if (!clip) return;

      if (video.src !== clip.source?.media_uri) {{
        video.src = clip.source?.media_uri || "";
      }}
      video.currentTime = sourceTimeFor(clip, time);
      if (playing) {{
        video.play().catch(() => undefined);
      }}
      clipTitle.textContent = `${{clip.clip_id}} · ${{clip.source?.author || ""}}`;
      clipDetail.textContent = `${{formatTime(clip.timeline_in)}}-${{formatTime(clip.timeline_out)}} · 源 ${{clip.source?.analysis_window || ""}} · ${{(clip.source?.tags || []).slice(0, 4).join(" / ")}}`;
      renderLogoOverlay(clip);
      renderAuthorOverlay(clip);
      setStatus(clip.edit_note || clip.source?.video_path || "");
      document.querySelectorAll(".clip-row").forEach((row, rowIndex) => row.classList.toggle("active", rowIndex === index));
      document.querySelectorAll(".bar").forEach((bar, barIndex) => bar.classList.toggle("active", barIndex === index));
    }}

    function sourceTimeFor(clip, timelineTime) {{
      const sourceIn = Number(clip.source?.source_in || 0);
      const timelineIn = Number(clip.timeline_in || 0);
      return Math.max(0, sourceIn + Math.max(0, timelineTime - timelineIn));
    }}

    function findClipIndex(time) {{
      if (!timeline.length) return -1;
      const lastIndex = timeline.length - 1;
      return timeline.findIndex((clip, index) => {{
        const start = Number(clip.timeline_in || 0);
        const end = Number(clip.timeline_out || 0);
        return time >= start && (time < end || (index === lastIndex && time <= end));
      }});
    }}

    function updateUi(time) {{
      scrubber.value = String(clamp(time, 0, totalDuration));
      if (currentTimeLabel) currentTimeLabel.textContent = formatTime(time);
    }}

    function setStatus(text, isError = false) {{
      statusEl.textContent = text || "";
      statusEl.classList.toggle("error", isError);
    }}

    function formatTime(value) {{
      const safe = Math.max(0, Number(value || 0));
      const minutes = Math.floor(safe / 60);
      const seconds = Math.floor(safe % 60);
      const millis = Math.floor((safe - Math.floor(safe)) * 1000);
      return `${{String(minutes).padStart(2, "0")}}:${{String(seconds).padStart(2, "0")}}.${{String(millis).padStart(3, "0")}}`;
    }}

    function clamp(value, min, max) {{
      return Math.min(max, Math.max(min, Number(value || 0)));
    }}

    function escapeHtml(value) {{
      return String(value || "").replace(/[&<>"']/g, (char) => ({{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      }}[char]));
    }}
  </script>
</body>
</html>
"""
    return html


def default_preview_output(plan_path: Path) -> Path:
    if plan_path.name == "plan.json":
        return plan_path.parent / "preview.html"
    return plan_path.with_suffix(".preview.html")


def default_proxy_dir(plan_path: Path) -> Path:
    if plan_path.name == "plan.json":
        return plan_path.parent / "preview_assets"
    return plan_path.with_suffix("").parent / f"{plan_path.stem}.preview_assets"


def generate_preview(
    plan_path: Path,
    output_path: Path | None = None,
    *,
    make_proxies: bool = False,
    proxy_dir: Path | None = None,
    overwrite_proxies: bool = False,
) -> Path:
    if not plan_path.exists():
        raise FileNotFoundError(f"剪辑方案不存在: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if make_proxies:
        resolved_proxy_dir = proxy_dir or default_proxy_dir(plan_path)
        enriched = build_proxy_plan(plan, plan_path, resolved_proxy_dir, overwrite=overwrite_proxies)
        enriched = _rewrite_media_uris_for_proxy(enriched, resolved_proxy_dir, plan_path, uri_prefix="preview_assets")
    else:
        enriched = _enrich_plan(plan, plan_path)
    html = build_preview_html(enriched, plan_path)
    _validate_preview_html(html)
    output = output_path or default_preview_output(plan_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output


def _validate_preview_html(html: str) -> None:
    if 'buffer.split("\n")' in html:
        raise ValueError('preview HTML 里出现了未转义的换行符，请使用 buffer.split("\\\\n")')


def _rewrite_media_uris_for_proxy(
    plan: dict[str, Any],
    proxy_dir: Path,
    plan_path: Path,
    *,
    uri_prefix: str,
) -> dict[str, Any]:
    served = json.loads(json.dumps(plan, ensure_ascii=False))
    prefix = uri_prefix.rstrip("/")
    if (proxy_dir / "bgm.m4a").exists():
        served.setdefault("bgm", {})["media_uri"] = f"{prefix}/bgm.m4a"

    for index, clip in enumerate(served.get("timeline") or [], start=1):
        clip_id = str(clip.get("clip_id") or f"c{index:03d}")
        proxy_path = proxy_dir / f"{clip_id}.mp4"
        if proxy_path.exists():
            clip.setdefault("source", {})["media_uri"] = f"{prefix}/{proxy_path.name}"

    assets = served.setdefault("assets", {})
    logo = assets.get("logo_overlay")
    if isinstance(logo, dict) and logo.get("enabled") and logo.get("image_path"):
        logo_path = Path(str(logo.get("image_path"))).expanduser()
        if not logo_path.is_absolute():
            logo_path = Path.cwd() / logo_path
        if logo_path.exists():
            served_logo = proxy_dir / f"logo_overlay{logo_path.suffix.lower() or '.png'}"
            if not served_logo.exists() or served_logo.stat().st_mtime < logo_path.stat().st_mtime:
                shutil.copy2(logo_path, served_logo)
            logo["media_uri"] = f"{prefix}/{served_logo.name}"

    return served


def _rewrite_media_uris_for_server(plan: dict[str, Any], proxy_dir: Path, plan_path: Path) -> dict[str, Any]:
    return _rewrite_media_uris_for_proxy(plan, proxy_dir, plan_path, uri_prefix="/preview_assets")


def default_export_dir(plan_path: Path) -> Path:
    """Return the run-level output directory for exported finished videos."""
    resolved = plan_path.resolve()
    for parent in resolved.parents:
        if parent.name == "clip_plans":
            return parent.parent / "output"
    return resolved.parent / "output"


def default_export_output(plan_path: Path) -> Path:
    return default_export_dir(plan_path) / "output.mp4"


def export_clip_video(
    plan_path: Path,
    output_path: Path | None = None,
    *,
    verbose: bool = False,
    use_proxy: bool = True,
) -> Path:
    """Export clip plan as a composited MP4 video.
    
    Combines timeline clips, applies the same logo overlay rules as preview,
    and mixes with BGM. Preview proxy assets are generated/reused so source
    trimming, crop, and browser preview stay aligned with the exported video.
    """
    if not plan_path.exists():
        raise FileNotFoundError(f"剪辑方案不存在: {plan_path}")
    
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    base_dir = plan_path.resolve().parent
    proxy_dir = default_proxy_dir(plan_path)
    
    timeline = plan.get("timeline") or []
    if not timeline:
        raise ValueError("时间线为空，无法导出视频")
    
    bgm = plan.get("bgm") or {}
    
    if use_proxy:
        plan = build_proxy_plan(plan, plan_path, proxy_dir, overwrite=False)
        timeline = plan.get("timeline") or []
        if verbose:
            print(f"[INFO] 使用预览代理资源: {proxy_dir}", file=sys.stderr)
        
        bgm_path = proxy_dir / "bgm.m4a"
        if not bgm_path.exists():
            raise FileNotFoundError(f"预览BGM不存在: {bgm_path}")

        concat_entries = []
        for index, clip in enumerate(timeline, start=1):
            clip_id = str(clip.get("clip_id") or f"c{index:03d}")
            clip_proxy = proxy_dir / f"{clip_id}.mp4"
            if not clip_proxy.exists():
                # Try alternate naming patterns
                clip_proxy_alt = proxy_dir / f"clip_{index:03d}.mp4"
                if clip_proxy_alt.exists():
                    clip_proxy = clip_proxy_alt
                else:
                    raise FileNotFoundError(f"预览视频不存在: {clip_proxy} 或 {clip_proxy_alt}")
            
            concat_entries.append(f"file '{clip_proxy.resolve().as_posix()}'")
    else:
        # Fallback mode: useful for debugging only. It assumes sources already
        # match the desired clip durations and codecs.
        bgm_path = Path(str(bgm.get("audio_path") or "")).expanduser()
        if not bgm_path.is_absolute():
            bgm_path = base_dir / bgm_path
        
        if not bgm_path.exists():
            raise FileNotFoundError(f"BGM文件不存在: {bgm_path}")
        
        concat_entries = []
        for clip in timeline:
            source = clip.get("source") or {}
            video_path = Path(str(source.get("video_path") or "")).expanduser()
            if not video_path.is_absolute():
                video_path = base_dir / video_path
            
            if not video_path.exists():
                raise FileNotFoundError(f"视频文件不存在: {video_path}")
            
            concat_entries.append(f"file '{video_path.resolve().as_posix()}'")
    
    output = output_path or default_export_output(plan_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    
    # Create concat demuxer file
    work_dir = output.parent / f".export_work_{output.stem}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    concat_file = work_dir / "concat_list.txt"
    concat_file.write_text("\n".join(concat_entries) + "\n", encoding="utf-8")
    
    # Use FFmpeg concat demuxer to combine clips
    total_duration = float(plan.get("output", {}).get("duration_seconds") or bgm.get("duration_seconds") or 0.0)
    
    # First pass: concatenate all videos without audio
    temp_video = work_dir / "temp_concat.mp4"
    concat_cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c:v", "copy",
        "-an",
        str(temp_video),
    ]
    
    if verbose:
        print(f"[CONCAT] {' '.join(concat_cmd)}", file=sys.stderr)
    
    try:
        subprocess.run(concat_cmd, check=True, capture_output=not verbose)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg concat失败: {e}") from e
    
    # Second pass: mix video with audio and apply the same visual canvas as preview.
    base_vf = (
        "scale='if(gt(a,9/16),-2,1080)':'if(gt(a,9/16),1920,-2)',"
        "crop=1080:1920,setsar=1"
    )
    filter_complex, output_video_label, logo_input = _build_export_video_filter(plan, base_vf, base_dir)
    
    final_cmd = [
        "ffmpeg",
        "-y",
        "-i", str(temp_video),
    ]
    if logo_input:
        final_cmd.extend(["-loop", "1", "-i", str(logo_input)])
    final_cmd.extend([
        "-i", str(bgm_path),
    ])
    if filter_complex:
        final_cmd.extend(["-filter_complex", filter_complex, "-map", output_video_label])
    else:
        final_cmd.extend(["-vf", base_vf, "-map", "0:v:0"])
    final_cmd.extend([
        "-map", f"{2 if logo_input else 1}:a:0",
        "-t", f"{total_duration:.3f}",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-preset", "medium",
        "-crf", "22",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(output),
    ])
    
    if verbose:
        print(f"[EXPORT] {' '.join(final_cmd)}", file=sys.stderr)
    
    try:
        subprocess.run(final_cmd, check=True, capture_output=not verbose)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg导出失败: {e}") from e
    
    # Cleanup temp files
    try:
        concat_file.unlink()
        temp_video.unlink()
        work_dir.rmdir()
    except Exception:
        pass  # Ignore cleanup errors
    
    return output


def _resolve_local_path(path_value: str, base_dir: Path) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        candidates = [Path.cwd() / path, base_dir / path]
        path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    return path


def _drawtext_font_path() -> Path | None:
    candidates = [
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    return next((path for path in candidates if path.exists()), None)


def _escape_drawtext_value(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("%", "\\%")
        .replace("\n", " ")
    )


def _build_export_video_filter(
    plan: dict[str, Any],
    base_vf: str,
    base_dir: Path,
) -> tuple[str, str, Path | None]:
    logo = (plan.get("assets") or {}).get("logo_overlay") or {}
    logo_path_value = str(logo.get("image_path") or "")
    logo_path = _resolve_local_path(logo_path_value, base_dir) if logo.get("enabled") and logo_path_value else None
    if logo_path and not logo_path.exists():
        logo_path = None

    filters = [f"[0:v]{base_vf}[base]"]
    current_label = "[base]"
    hidden_types = set(logo.get("hide_on_clip_types") or [])
    windows: list[tuple[float, float]] = []
    for clip in plan.get("timeline") or []:
        clip_type = str((clip.get("source") or {}).get("clip_type") or "")
        if clip_type in hidden_types:
            continue
        start = float(clip.get("timeline_in") or 0.0)
        end = float(clip.get("timeline_out") or 0.0)
        if end > start:
            windows.append((start, end))

    if logo_path and windows:
        enable_expr = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in windows)
        if logo.get("render_mode") == "full_canvas":
            logo_filter = "[1:v]scale=1080:1920,format=rgba[logo]"
            overlay_position = "0:0"
        else:
            logo_filter = "[1:v]format=rgba[logo]"
            position = str(logo.get("position") or "top_left")
            positions = {
                "top_left": "18:18",
                "top_right": "main_w-overlay_w-18:18",
                "bottom_left": "18:main_h-overlay_h-18",
                "bottom_right": "main_w-overlay_w-18:main_h-overlay_h-18",
                "bottom_center": "(main_w-overlay_w)/2:main_h-overlay_h-18",
            }
            overlay_position = positions.get(position, "18:18")
        filters.append(logo_filter)
        filters.append(f"{current_label}[logo]overlay={overlay_position}:eof_action=repeat:enable='{enable_expr}'[logoed]")
        current_label = "[logoed]"

    font_path = _drawtext_font_path()
    author_font_color = _author_color_from_logo(plan)
    output_width, output_height = _output_resolution(plan)
    author_overlay = (plan.get("assets") or {}).get("author_id_overlay") or {}
    author_font_size = max(24, round(output_width * float(author_overlay.get("font_size_percent") or 4.5) / 100))
    author_margin_x = max(24, round(output_width * float(author_overlay.get("margin_x_percent") or 4.45) / 100))
    author_y = max(36, round(output_height * float(author_overlay.get("top_percent") or 5.2) / 100))
    author_index = 0
    for clip in plan.get("timeline") or []:
        source = clip.get("source") or {}
        author = str(source.get("author") or "").strip()
        start = float(clip.get("timeline_in") or 0.0)
        end = float(clip.get("timeline_out") or 0.0)
        if not author or end <= start:
            continue
        author_index += 1
        next_label = f"[author{author_index}]"
        font_part = f"fontfile='{_escape_drawtext_value(str(font_path))}':" if font_path else ""
        text = _escape_drawtext_value(author)
        filters.append(
            f"{current_label}drawtext={font_part}"
            f"text='{text}':x=w-tw-{author_margin_x}:y={author_y}:"
            f"fontsize={author_font_size}:fontcolor={author_font_color}:"
            f"enable='between(t,{start:.3f},{end:.3f})'{next_label}"
        )
        current_label = next_label

    if current_label == "[base]":
        return "", "", logo_path
    return ";".join(filters), current_label, logo_path


class PreviewHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP request handler for preview server with export support."""
    
    plan_path: Path | None = None
    preview_path: Path | None = None
    verbose: bool = False
    
    def do_GET(self) -> None:
        """Handle GET requests for preview HTML."""
        parsed_url = urlparse(self.path)
        request_path = parsed_url.path
        query = parse_qs(parsed_url.query)

        if request_path == "/" or request_path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if self.preview_path and self.preview_path.exists():
                self.wfile.write(self.preview_path.read_bytes())
            return

        if request_path.startswith("/exports/"):
            output_name = Path(request_path.removeprefix("/exports/")).name
            export_dir = default_export_dir(self.plan_path)
            export_path = (export_dir / output_name).resolve()
            if export_path.exists() and export_path.is_relative_to(export_dir):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                if query.get("download"):
                    self.send_header("Content-Disposition", f'attachment; filename="{export_path.name}"')
                self.end_headers()
                with open(export_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
                return
        
        # Serve proxy assets
        if request_path.startswith("/preview_assets/"):
            asset_path = (self.plan_path.parent / request_path[1:]).resolve()
            if asset_path.exists() and asset_path.is_relative_to(self.plan_path.parent):
                with open(asset_path, "rb") as f:
                    self.send_response(200)
                    if request_path.endswith(".mp4"):
                        self.send_header("Content-Type", "video/mp4")
                    elif request_path.endswith(".m4a"):
                        self.send_header("Content-Type", "audio/mp4")
                    elif request_path.endswith(".json"):
                        self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(f.read())
                return
        
        self.send_response(404)
        self.end_headers()
    
    def do_POST(self) -> None:
        """Handle POST requests for export."""
        if self.path != "/api/export":
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success": false, "message": "Not found"}')
            return
        
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            request = json.loads(body)
            output_name = request.get("output", "output.mp4")
            output_name = Path(str(output_name)).name
            
            if not output_name.endswith(".mp4"):
                output_name = output_name + ".mp4"
            
            output_path = default_export_dir(self.plan_path) / output_name
            
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            
            # Custom subprocess to track progress
            self._send_sse_event({"status": "info", "message": f"开始导出到 {output_name}...", "progress": 10})
            
            try:
                output = export_clip_video(
                    self.plan_path,
                    output_path,
                    verbose=self.verbose,
                )
                output_url = f"/exports/{output.name}"
                self._send_sse_event({
                    "status": "success",
                    "message": "✓ 导出完成！",
                    "progress": 100,
                    "path": str(output),
                    "url": output_url,
                    "download_url": f"{output_url}?download=1",
                })
            except Exception as e:
                self._send_sse_event({"status": "error", "message": f"✗ 导出失败: {str(e)}", "progress": 0})
        
        except Exception as error:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "message": str(error)}, ensure_ascii=False).encode("utf-8"))
    
    def _send_sse_event(self, event: dict[str, Any]) -> None:
        """Send Server-Sent Event."""
        data = json.dumps(event, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()
    
    def log_message(self, format: str, *args: Any) -> None:
        """Suppress log messages."""
        if self.verbose:
            super().log_message(format, *args)


def serve_preview(
    plan_path: Path,
    port: int = 8866,
    make_proxies: bool = False,
    proxy_dir: Path | None = None,
    overwrite_proxies: bool = False,
    verbose: bool = False,
) -> None:
    """Start an interactive preview server."""
    if not plan_path.exists():
        raise FileNotFoundError(f"剪辑方案不存在: {plan_path}")
    
    # Generate or update preview
    resolved_proxy_dir = proxy_dir or default_proxy_dir(plan_path)
    if make_proxies:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = build_proxy_plan(plan, plan_path, resolved_proxy_dir, overwrite=overwrite_proxies)
        plan = _rewrite_media_uris_for_server(plan, resolved_proxy_dir, plan_path)
    else:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = _enrich_plan(plan, plan_path)
    
    preview_path = default_preview_output(plan_path)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_preview_html(plan, plan_path)
    _validate_preview_html(html)
    preview_path.write_text(html, encoding="utf-8")
    
    # Setup handler
    PreviewHTTPRequestHandler.plan_path = plan_path.resolve()
    PreviewHTTPRequestHandler.preview_path = preview_path.resolve()
    PreviewHTTPRequestHandler.verbose = verbose
    
    # Start server
    server = http.server.HTTPServer(("127.0.0.1", port), PreviewHTTPRequestHandler)
    url = f"http://127.0.0.1:{port}"
    
    print(f"[OK] 预览服务已启动: {url}", file=sys.stderr)
    print(f"[OK] 按 Ctrl+C 退出", file=sys.stderr)
    
    # Open browser in background thread
    def open_browser() -> None:
        import time
        time.sleep(1)  # Wait for server to fully start
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"[WARN] 无法自动打开浏览器: {e}", file=sys.stderr)
    
    threading.Thread(target=open_browser, daemon=True).start()
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[OK] 预览服务已停止", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a browser preview HTML or export video for a Rednote clip plan")
    parser.add_argument("plan", nargs="?", default=str(DEFAULT_PLAN), help="clip plan JSON path")
    parser.add_argument("--output", "-o", default="", help="output path (HTML or MP4); defaults based on mode")
    parser.add_argument("--mode", choices=["preview", "export", "serve"], default="preview", help="generation mode: preview (HTML), export (MP4), or serve (interactive server)")
    parser.add_argument("--make-proxies", action="store_true", help="transcode timeline clips to browser-safe H.264 preview assets")
    parser.add_argument("--proxy-dir", default="", help="directory for generated preview assets")
    parser.add_argument("--overwrite-proxies", action="store_true", help="regenerate existing preview assets")
    parser.add_argument("--verbose", "-v", action="store_true", help="verbose output")
    parser.add_argument("--port", type=int, default=8866, help="port for serve mode (default: 8866)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        if args.mode == "serve":
            serve_preview(
                Path(args.plan),
                port=args.port,
                make_proxies=args.make_proxies,
                proxy_dir=Path(args.proxy_dir) if args.proxy_dir else None,
                overwrite_proxies=args.overwrite_proxies,
                verbose=args.verbose,
            )
        elif args.mode == "export":
            output = export_clip_video(
                Path(args.plan),
                Path(args.output) if args.output else None,
                verbose=args.verbose,
            )
            print(f"[OK] 视频已导出: {output}", file=sys.stderr)
        else:  # preview
            output = generate_preview(
                Path(args.plan),
                Path(args.output) if args.output else None,
                make_proxies=args.make_proxies,
                proxy_dir=Path(args.proxy_dir) if args.proxy_dir else None,
                overwrite_proxies=args.overwrite_proxies,
            )
            print(f"[OK] 浏览器预览页已生成: {output}", file=sys.stderr)
    except Exception as exc:
        raise SystemExit(f"[ERROR] {exc}") from exc


if __name__ == "__main__":
    main()
