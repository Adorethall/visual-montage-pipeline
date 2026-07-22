from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from visual_montage.author_overlay import (
    author_center_transform_x,
    author_rich_spans,
    author_text_style,
)


DEFAULT_SKILL_ROOT = Path("/Users/linying/tools/jianying-editor-skill")
SKILL_ROOT = Path(os.getenv("JY_SKILL_ROOT", str(DEFAULT_SKILL_ROOT))).expanduser()
if not (SKILL_ROOT / "scripts" / "jy_wrapper.py").exists():
    raise SystemExit(
        f"Jianying skill not found: {SKILL_ROOT}. "
        "Install it in /Users/linying/tools/jianying-editor-skill or set JY_SKILL_ROOT."
    )

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
sys.path.insert(0, str(SKILL_ROOT / "scripts" / "vendor"))

from jy_wrapper import JyProject  # noqa: E402
import pyJianYingDraft as draft  # noqa: E402


DEFAULT_PLAN = Path("data/rednote_workspace/runs/20260708_163637/clip_plans/llm-strategy/plan.json")
DEFAULT_DRAFTS_ROOT = Path("/Users/linying/Movies/JianyingPro/User Data/Projects/com.lveditor.draft")
DEFAULT_MEDIA_ROOT = Path(os.getenv("REDNOTE_JIANYING_MEDIA_ROOT", "/Users/linying/Movies/JianyingPro/RednoteMedia"))


def _canvas_dimensions(aspect_ratio: str) -> tuple[int, int]:
    normalized = str(aspect_ratio or "9:16").strip().replace("：", ":")
    return {
        "1:1": (1080, 1080),
        "16:9": (1920, 1080),
        "4:5": (1080, 1350),
        "9:16": (1080, 1920),
    }.get(normalized, (1080, 1920))


def _fill_canvas(seg: Any, canvas_width: int, canvas_height: int) -> None:
    """Center-crop a visual segment by scaling it until it fills the canvas."""
    width, height = getattr(seg, "material_size", (0, 0))
    if not width or not height:
        return
    source_ratio = width / height
    canvas_ratio = canvas_width / canvas_height
    scale = source_ratio / canvas_ratio if source_ratio >= canvas_ratio else canvas_ratio / source_ratio
    seg.clip_settings.scale_x = scale
    seg.clip_settings.scale_y = scale
    seg.uniform_scale = True


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    for candidate in (Path.cwd() / path, base_dir / path):
        if candidate.exists():
            return candidate.resolve()
    return (Path.cwd() / path).resolve()


def _bundle_media(path: Path, media_dir: Path) -> Path:
    """Copy draft media into a Jianying-readable, task-stable directory."""
    path = path.resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    target = media_dir / f"{digest}_{path.name}"
    if not target.exists() or target.stat().st_size != path.stat().st_size:
        shutil.copy2(path, target)
    return target


def _normalize_opaque_mov_for_jianying(path: Path, media_dir: Path) -> Path:
    """Transcode opaque QTRLE/MOV assets to a JianYing-stable CFR MP4."""
    path = path.resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(
        (str(path) + str(path.stat().st_mtime_ns) + "|h264-cfr30-v1").encode()
    ).hexdigest()[:10]
    output = media_dir / f"{digest}_{path.stem}.jianying.mp4"
    if output.exists() and output.stat().st_size > 0:
        return output
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path), "-an", "-vf", "fps=30",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
        ],
        check=True,
    )
    return output


def _extract_tail_freeze_frame(path: Path, media_dir: Path) -> Path:
    path = path.resolve()
    digest = hashlib.sha1(
        (str(path) + str(path.stat().st_mtime_ns) + "|tail-freeze-v1").encode()
    ).hexdigest()[:10]
    output = media_dir / f"{digest}_{path.stem}.tail-freeze.png"
    if output.exists() and output.stat().st_size > 0:
        return output
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "1.000", "-i", str(path), "-frames:v", "1", str(output),
        ],
        check=True,
    )
    return output


def _load_asset_library(
    plan: dict[str, Any],
    base_dir: Path,
) -> tuple[dict[str, Any], Path | None]:
    assets = plan.get("assets") or {}
    library_value = str(
        assets.get("asset_library_path")
        or plan.get("asset_library_path")
        or "data/assets/asset-library.yaml"
    )
    library_path = _resolve_path(library_value, base_dir)
    if not library_path.exists():
        return {}, None
    return yaml.safe_load(library_path.read_text(encoding="utf-8")) or {}, library_path


def _registered_asset_path(
    plan: dict[str, Any],
    asset_id: str,
    base_dir: Path,
) -> Path | None:
    if not asset_id:
        return None
    library, library_path = _load_asset_library(plan, base_dir)
    if not library_path:
        return None
    for section in (
        "brand_assets",
        "product_openpages",
        "product_recordings",
        "overlay_assets",
        "fonts",
    ):
        for item in library.get(section) or []:
            if item.get("asset_id") == asset_id and item.get("path"):
                return _resolve_path(str(item["path"]), library_path.parent)
    return None


def _infer_logo_sequence(
    plan: dict[str, Any],
    base_dir: Path,
    logo: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    assets = plan.get("assets") or {}
    explicit_intro = assets.get("logo_intro_overlay")
    explicit_cover = assets.get("cover_logo_overlay")
    if isinstance(explicit_intro, dict) and isinstance(explicit_cover, dict):
        return explicit_intro, explicit_cover

    library, library_path = _load_asset_library(plan, base_dir)
    brand_assets = library.get("brand_assets") or []
    static_asset = None
    static_id = str(logo.get("asset_id") or "")
    static_path = str(logo.get("image_path") or "")
    static_name = Path(static_path).name if static_path else ""
    for item in brand_assets:
        if item.get("type") != "logo_overlay":
            continue
        if static_id and item.get("asset_id") == static_id:
            static_asset = item
            break
        if static_name and Path(str(item.get("path") or "")).name == static_name:
            static_asset = item
            break

    animated_asset = None
    if static_asset:
        color = static_asset.get("color_variant")
        animated_asset = next(
            (
                item
                for item in brand_assets
                if item.get("type") == "logo_overlay_video"
                and item.get("color_variant") == color
            ),
            None,
        )

    intro = explicit_intro if isinstance(explicit_intro, dict) else {
        "enabled": bool(animated_asset),
        "asset_id": (animated_asset or {}).get("asset_id", ""),
    }
    cover = explicit_cover if isinstance(explicit_cover, dict) else {
        "enabled": bool(static_asset or static_path),
        "asset_id": (static_asset or {}).get("asset_id", static_id),
        "image_path": static_path,
    }
    return intro, cover


def _overlay_asset_path(
    plan: dict[str, Any],
    overlay: dict[str, Any],
    base_dir: Path,
    path_key: str,
) -> Path | None:
    path_value = str(overlay.get(path_key) or "")
    path = _resolve_path(path_value, base_dir) if path_value else _registered_asset_path(
        plan,
        str(overlay.get("asset_id") or ""),
        base_dir,
    )
    return path if path and path.exists() else None


def _eligible_logo_clips(plan: dict[str, Any], hidden_types: set[str]) -> list[dict[str, Any]]:
    return [
        clip
        for clip in (plan.get("timeline") or [])
        if str((clip.get("source") or {}).get("clip_type") or "") not in hidden_types
        and _seconds(clip.get("duration")) > 0
    ]


def _seconds(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _time(value: float) -> str:
    return f"{max(0.0, value):.3f}s"


def _microseconds(value: Any) -> int:
    """Convert seconds to integer microseconds without string/float parser drift."""
    return max(0, int(round(_seconds(value) * 1_000_000)))


def _add_audio_slice(
    project: Any,
    media_path: Path,
    *,
    target_start: float,
    source_start: float,
    duration: float,
    track_name: str,
    volume: float,
) -> Any:
    if duration <= 0:
        return None
    material = draft.AudioMaterial(str(media_path))
    segment = draft.AudioSegment(
        material,
        draft.trange(_microseconds(target_start), _microseconds(duration)),
        source_timerange=draft.trange(
            _microseconds(source_start),
            _microseconds(duration),
        ),
    )
    segment.volume = volume
    project._ensure_track(draft.TrackType.audio, track_name)
    project.script.add_segment(segment, track_name)
    return segment


def _voiceover_ranges(plan: dict[str, Any]) -> list[tuple[float, float]]:
    cta = ((plan.get("assets") or {}).get("cta_voiceover") or {})
    lines = cta.get("lines") or []
    if not cta.get("enabled") or not lines:
        return []
    timeline = plan.get("timeline") or []
    openpage = next(
        (
            clip for clip in timeline
            if ((clip.get("source") or {}).get("clip_type") == "fixed_openpage")
        ),
        None,
    )
    screenshot = next(
        (
            clip for clip in timeline
            if ((clip.get("source") or {}).get("clip_type") == "fixed_screenshot")
        ),
        None,
    )
    tail = next(
        (
            clip for clip in timeline
            if ((clip.get("source") or {}).get("clip_type") == "fixed_tail_sticker")
        ),
        None,
    )
    gap = 0.10
    durations = [
        _seconds(line.get("audio_seconds"))
        for line in lines
        if _seconds(line.get("audio_seconds")) > 0
    ]
    if not durations:
        return []
    total = sum(durations) + gap * max(0, len(durations) - 1)
    product_start = _seconds(
        (openpage or screenshot or {}).get("timeline_in"),
        -1.0,
    )
    tail_start = _seconds(
        (tail or {}).get("timeline_in"),
        _seconds((plan.get("output") or {}).get("duration_seconds")),
    )
    cursor = product_start if product_start >= 0 else max(0.0, tail_start - total)
    output = []
    for duration in durations:
        output.append((cursor, min(tail_start, cursor + duration)))
        cursor += duration + gap
    return [(start, end) for start, end in output if end > start]


def _probe_duration_seconds(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return max(0.0, float(result.stdout.strip()))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0.0


def _repair_small_video_timeline_gaps(
    plan: dict[str, Any],
    base_dir: Path,
    *,
    maximum_repair_seconds: float = 0.75,
) -> list[dict[str, Any]]:
    """Prevent JianYing magnetic-track shifts by closing small legacy gaps."""
    timeline = sorted(
        plan.get("timeline") or [],
        key=lambda clip: _seconds(clip.get("timeline_in")),
    )
    repairs = []
    for left, right in zip(timeline, timeline[1:]):
        left_end = _seconds(left.get("timeline_out"))
        right_start = _seconds(right.get("timeline_in"))
        gap = right_start - left_end
        if gap <= 0.001:
            continue
        source = left.get("source") or {}
        if (
            str(source.get("clip_type") or "") != "highlight"
            or gap > maximum_repair_seconds
        ):
            raise ValueError(
                f"unrepairable main-track gap {gap:.3f}s between "
                f"{left.get('clip_id')} and {right.get('clip_id')}"
            )
        media_path = _resolve_path(
            str(source.get("video_path") or ""), base_dir
        )
        media_duration = _probe_duration_seconds(media_path)
        old_duration = _seconds(
            left.get("duration"),
            left_end - _seconds(left.get("timeline_in")),
        )
        new_duration = old_duration + gap
        source_in = _seconds(source.get("source_in"))
        if source_in + new_duration > media_duration + 0.001:
            source_in = max(0.0, media_duration - new_duration)
        if source_in + new_duration > media_duration + 0.001:
            raise ValueError(
                f"source too short to repair {left.get('clip_id')}: "
                f"need {new_duration:.3f}s, media has {media_duration:.3f}s"
            )
        left["timeline_out"] = right_start
        left["duration"] = new_duration
        source["source_in"] = source_in
        source["source_out"] = source_in + new_duration
        repairs.append({
            "clip_id": left.get("clip_id"),
            "next_clip_id": right.get("clip_id"),
            "gap_seconds": round(gap, 3),
            "old_timeline_out": round(left_end, 3),
            "new_timeline_out": round(right_start, 3),
            "new_duration": round(new_duration, 3),
        })
    plan["timeline"] = timeline
    return repairs


def _hex_to_rgb01(value: str) -> tuple[float, float, float]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        return 1.0, 1.0, 1.0
    return (
        int(value[0:2], 16) / 255.0,
        int(value[2:4], 16) / 255.0,
        int(value[4:6], 16) / 255.0,
    )


def _split_cover_title(title: str) -> str:
    raw_text = str(title or "").strip().replace("\\n", "\n").replace("|", "\n")
    paragraphs = [" ".join(part.split()) for part in raw_text.splitlines()]
    return "\n".join(part for part in paragraphs if part) or "Highlight"


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


def _cover_style_from_plan(plan: dict[str, Any], base_dir: Path) -> tuple[str, tuple[float, float, float]]:
    cover_clip = next(
        (
            clip
            for clip in plan.get("timeline") or []
            if ((clip.get("source") or {}).get("clip_type") == "fixed_cover")
        ),
        None,
    )
    cover = ((cover_clip or {}).get("cover") or {}) or ((plan.get("assets") or {}).get("cover") or {})
    title = _split_cover_title(str(cover.get("title") or "Highlight"))
    source = (cover_clip or {}).get("source") or {}
    video_path_value = str(source.get("video_path") or "")
    if video_path_value:
        video_path = _resolve_path(video_path_value, base_dir)
        if video_path.exists():
            accent = _sample_frame_theme_color(video_path, _seconds(source.get("source_in")))
            return title, _hex_to_rgb01(accent)
    return title, _hex_to_rgb01("F6E76B")


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _cover_font_from_plan(plan: dict[str, Any], title: str) -> draft.FontType:
    cover_clip = next(
        (
            clip
            for clip in plan.get("timeline") or []
            if ((clip.get("source") or {}).get("clip_type") == "fixed_cover")
        ),
        None,
    )
    cover = ((cover_clip or {}).get("cover") or {}) or ((plan.get("assets") or {}).get("cover") or {})
    language = str(cover.get("language") or "").strip().lower()
    if language.startswith(("zh", "cn")) or "中文" in language or _has_cjk(title):
        return draft.FontType.HarmonyOS_Sans_SC_Bold
    return draft.FontType.Inter_SemiBold


def _extract_audio(source_path: Path, output_dir: Path, *, duration_seconds: float) -> Path | None:
    if source_path.suffix.lower() in {".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg"}:
        return source_path
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{source_path.stem}.bgm.m4a"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-t",
        f"{max(0.1, duration_seconds):.3f}",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        return None
    return output_path


def export_plan(
    plan_path: Path, draft_name: str, drafts_root: Path, *, overwrite: bool,
    media_root: Path = DEFAULT_MEDIA_ROOT,
) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    base_dir = plan_path.parent
    timeline_repairs = _repair_small_video_timeline_gaps(plan, base_dir)
    media_dir = media_root / draft_name
    aspect_ratio = os.getenv("REDNOTE_OUTPUT_ASPECT_RATIO") or str((plan.get("output") or {}).get("aspect_ratio") or "9:16")
    canvas_width, canvas_height = _canvas_dimensions(aspect_ratio)

    project = JyProject(
        draft_name,
        width=canvas_width,
        height=canvas_height,
        drafts_root=str(drafts_root),
        overwrite=overwrite,
    )

    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    original_audio_ranges: list[tuple[float, float]] = []
    tail_freezes: list[dict[str, Any]] = []

    for clip in plan.get("timeline") or []:
        source = clip.get("source") or {}
        clip_type = str(source.get("clip_type") or "")
        media_path = _resolve_path(str(source.get("video_path") or ""), base_dir)
        original_media_path = media_path
        duration = _seconds(clip.get("duration"))
        if not media_path.exists() or duration <= 0:
            skipped.append(
                {
                    "clip_id": clip.get("clip_id"),
                    "reason": "missing_media_or_invalid_duration",
                    "path": str(media_path),
                }
            )
            continue
        if clip_type == "fixed_tail_sticker" and media_path.suffix.lower() == ".mov":
            media_path = _normalize_opaque_mov_for_jianying(media_path, media_dir)
        else:
            media_path = _bundle_media(media_path, media_dir)

        seg = project.add_clip(
            str(media_path),
            source_start=_microseconds(source.get("source_in")),
            duration=_microseconds(duration),
            target_start=_microseconds(clip.get("timeline_in")),
            track_name="VideoTrack",
        )
        if seg is None:
            skipped.append({"clip_id": clip.get("clip_id"), "reason": "add_clip_failed", "path": str(media_path)})
            continue
        _fill_canvas(seg, canvas_width, canvas_height)
        keep_original_audio = (
            source.get("clip_type") == "quality_15s"
            or source.get("audio_recommendation") == "original"
        )
        seg.volume = 1.0 if keep_original_audio else 0.0
        if keep_original_audio:
            original_audio_ranges.append((
                _seconds(clip.get("timeline_in")),
                _seconds(clip.get("timeline_out")),
            ))
        if clip_type == "fixed_tail_sticker":
            tail_freezes.append({
                "source_path": original_media_path,
                "timeline_in": _seconds(clip.get("timeline_in")),
                "timeline_out": _seconds(clip.get("timeline_out")),
            })
        added.append(
            {
                "clip_id": clip.get("clip_id"),
                "type": source.get("clip_type"),
                "path": str(media_path),
                "target_start": clip.get("timeline_in"),
                "source_start": source.get("source_in"),
                "duration": duration,
                "normalized_for_jianying": (
                    clip_type == "fixed_tail_sticker"
                    and media_path.suffix.lower() == ".mp4"
                ),
            }
        )

    tail_freeze_seconds = max(
        0.0,
        _seconds(
            ((plan.get("assets") or {}).get("endcard_freeze_last_seconds")),
            0.0,
        ),
    )
    for index, tail in enumerate(tail_freezes, 1):
        freeze_duration = min(
            tail_freeze_seconds,
            max(0.0, tail["timeline_out"] - tail["timeline_in"]),
        )
        if freeze_duration <= 0:
            continue
        try:
            freeze_path = _extract_tail_freeze_frame(
                Path(tail["source_path"]), media_dir
            )
            freeze_start = tail["timeline_out"] - freeze_duration
            freeze_seg = project.add_media_safe(
                str(freeze_path),
                start_time=_microseconds(freeze_start),
                duration=_microseconds(freeze_duration),
                track_name="EndcardFreezeOverlay",
            )
            if freeze_seg is None:
                raise RuntimeError("add_media_safe returned None")
            _fill_canvas(freeze_seg, canvas_width, canvas_height)
            freeze_seg.volume = 0.0
            added.append({
                "clip_id": f"endcard_freeze_{index:02d}",
                "type": "endcard_freeze_overlay",
                "path": str(freeze_path),
                "target_start": freeze_start,
                "duration": freeze_duration,
            })
        except Exception as exc:
            skipped.append({
                "clip_id": f"endcard_freeze_{index:02d}",
                "reason": f"endcard_freeze_failed: {exc}",
                "path": str(tail["source_path"]),
            })

    assets = plan.get("assets") or {}
    logo = assets.get("logo_overlay") or {}
    intro_logo, cover_logo = _infer_logo_sequence(plan, base_dir, logo)
    cover = assets.get("cover") or {}
    cover_duration = (
        _seconds(cover.get("duration_seconds"))
        if cover.get("enabled")
        else 0.0
    )
    cover_frame_value = str(cover.get("frame_path") or "")
    cover_frame_path = (
        _resolve_path(cover_frame_value, base_dir) if cover_frame_value else None
    )
    if cover_duration > 0 and cover_frame_path and cover_frame_path.is_file():
        bundled_cover_frame = _bundle_media(cover_frame_path, media_dir)
        cover_frame_segment = project.add_media_safe(
            str(bundled_cover_frame),
            start_time="0s",
            duration=_microseconds(cover_duration),
            track_name="CoverFrameOverlay",
        )
        if cover_frame_segment is not None:
            _fill_canvas(cover_frame_segment, canvas_width, canvas_height)
            cover_frame_segment.volume = 0.0
            added.append({
                "clip_id": "cover_frame_overlay",
                "type": "cover_frame_overlay",
                "path": str(bundled_cover_frame),
                "target_start": 0.0,
                "duration": cover_duration,
            })
        else:
            skipped.append({
                "clip_id": "cover_frame_overlay",
                "reason": "add_media_safe_failed",
                "path": str(cover_frame_path),
            })
    elif cover_duration > 0:
        skipped.append({
            "clip_id": "cover_frame_overlay",
            "reason": "missing_cover_frame",
            "path": str(cover_frame_path or ""),
        })
    intro_enabled = bool(intro_logo.get("enabled"))
    cover_logo_enabled = bool(cover_logo.get("enabled"))
    if logo.get("enabled"):
        logo_path = _overlay_asset_path(plan, logo, base_dir, "image_path")
        hidden_types = set(logo.get("hide_on_clip_types") or [])
        endcard_start = min(
            (
                _seconds(clip.get("timeline_in"))
                for clip in plan.get("timeline") or []
                if str((clip.get("source") or {}).get("clip_type") or "")
                == "fixed_tail_sticker"
            ),
            default=float("inf"),
        )
        logo_end_guard = max(
            0.0,
            _seconds(logo.get("end_guard_seconds"), 0.0),
        )
        logo_hard_end = endcard_start - logo_end_guard
        if logo_path:
            logo_path = _bundle_media(logo_path, media_dir)
            logo_count = 0
            eligible = _eligible_logo_clips(plan, hidden_types)
            first_clip = eligible[0] if eligible else None
            for clip in eligible:
                duration = _seconds(clip.get("duration"))
                start = _seconds(clip.get("timeline_in"))
                if logo_hard_end != float("inf"):
                    duration = min(duration, max(0.0, logo_hard_end - start))
                if clip is first_clip and (intro_enabled or cover_logo_enabled):
                    if cover_logo_enabled or cover_duration <= 0:
                        continue
                    duration = min(duration, cover_duration)
                if duration <= 0:
                    continue
                seg = project.add_media_safe(
                    str(logo_path),
                    start_time=_microseconds(start),
                    duration=_microseconds(duration),
                    track_name="LogoOverlay",
                )
                if seg is not None:
                    _fill_canvas(seg, canvas_width, canvas_height)
                    logo_count += 1
            added.append(
                {
                    "clip_id": "logo_overlay",
                    "type": "logo_overlay",
                    "path": str(logo_path),
                    "segments": logo_count,
                    "hard_end_seconds": (
                        None if logo_hard_end == float("inf")
                        else round(logo_hard_end, 3)
                    ),
                    "end_guard_seconds": round(logo_end_guard, 3),
                }
            )
        else:
            skipped.append({"clip_id": "logo_overlay", "reason": "missing_logo", "path": str(logo_path)})

    logo_hidden_types = set(
        (logo.get("hide_on_clip_types") or [])
        or ["fixed_openpage", "fixed_screenshot", "fixed_tail_sticker"]
    )
    eligible = _eligible_logo_clips(plan, logo_hidden_types)
    first_clip = eligible[0] if eligible else None

    if cover_logo_enabled and first_clip and cover_duration > 0:
        cover_logo_path = _overlay_asset_path(plan, cover_logo, base_dir, "image_path")
        duration = min(_seconds(first_clip.get("duration")), cover_duration)
        if cover_logo_path and duration > 0:
            cover_logo_path = _bundle_media(cover_logo_path, media_dir)
            seg = project.add_media_safe(
                str(cover_logo_path),
                start_time=_microseconds(first_clip.get("timeline_in")),
                duration=_microseconds(duration),
                track_name="CoverLogoOverlay",
            )
            if seg is not None:
                _fill_canvas(seg, canvas_width, canvas_height)
                added.append({
                    "clip_id": "cover_logo_overlay",
                    "type": "cover_logo_overlay",
                    "path": str(cover_logo_path),
                    "segments": 1,
                })
        else:
            skipped.append({
                "clip_id": "cover_logo_overlay",
                "reason": "missing_logo_or_invalid_duration",
                "path": str(cover_logo_path),
            })

    if intro_enabled and first_clip:
        intro_logo_path = _overlay_asset_path(plan, intro_logo, base_dir, "video_path")
        intro_start = _seconds(first_clip.get("timeline_in")) + cover_duration
        intro_duration = max(0.0, _seconds(first_clip.get("timeline_out")) - intro_start)
        if intro_logo_path and intro_duration > 0:
            intro_logo_path = _bundle_media(intro_logo_path, media_dir)
            media_duration = _probe_duration_seconds(intro_logo_path)
            if media_duration > 0:
                intro_duration = min(intro_duration, media_duration)
            seg = project.add_clip(
                str(intro_logo_path),
                source_start=0,
                duration=_microseconds(intro_duration),
                target_start=_microseconds(intro_start),
                track_name="LogoIntroOverlay",
            )
            if seg is not None:
                _fill_canvas(seg, canvas_width, canvas_height)
                seg.volume = 0.0
                added.append({
                    "clip_id": "logo_intro_overlay",
                    "type": "logo_intro_overlay",
                    "path": str(intro_logo_path),
                    "target_start": intro_start,
                    "duration": intro_duration,
                    "segments": 1,
                })
        else:
            skipped.append({
                "clip_id": "logo_intro_overlay",
                "reason": "missing_logo_or_no_time_after_cover",
                "path": str(intro_logo_path),
            })

    author_overlay = assets.get("author_id_overlay") or {}
    author_overlay_enabled = author_overlay.get("enabled") is not False
    author_font_size = float(author_overlay.get("font_size") or 4.8)
    author_emoji_scale = float(author_overlay.get("emoji_font_scale") or 1.0)
    author_right_edge_x = float(author_overlay.get("right_edge_transform_x") or 0.92)
    author_font_path = str(author_overlay.get("font_path") or "")
    author_transform_y = float(author_overlay.get("draft_transform_y") or 0.82)
    author_count = 0
    if author_overlay_enabled:
        for clip in plan.get("timeline") or []:
            source = clip.get("source") or {}
            clip_type = str(source.get("clip_type") or "")
            if clip_type not in {"highlight", "quality_15s"}:
                continue
            author = str(source.get("author") or "").strip()
            duration = _seconds(clip.get("duration"))
            if not author or duration <= 0:
                continue
            text_style = author_text_style(author, author_font_size)
            rich_spans = [
                draft.RichTextSpan(
                    item["start"],
                    item["end"],
                    size=item["size"],
                    bold=True,
                    color=(1.0, 1.0, 1.0),
                )
                for item in author_rich_spans(
                    author,
                    font_size=author_font_size,
                    emoji_scale=author_emoji_scale,
                )
            ]
            author_transform_x = author_center_transform_x(
                author,
                font_path=author_font_path,
                font_size=author_font_size,
                canvas_width=canvas_width,
                right_edge_transform_x=author_right_edge_x,
            )
            try:
                project.add_text_simple(
                    author,
                    start_time=_microseconds(clip.get("timeline_in")),
                    duration=_microseconds(duration),
                    track_name="AuthorID",
                    style=draft.TextStyle(
                        size=text_style["font_size"],
                        bold=True,
                        color=(1.0, 1.0, 1.0),
                        alpha=1.0,
                        align=2,
                        auto_wrapping=text_style["auto_wrapping"],
                        max_line_width=text_style["max_line_width"],
                    ),
                    border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.9, width=32.0),
                    shadow=draft.TextShadow(
                        color=(0.0, 0.0, 0.0),
                        alpha=0.45,
                        diffuse=6.0,
                        distance=4.0,
                        angle=-45.0,
                    ),
                    rich_spans=rich_spans,
                    clip_settings=draft.ClipSettings(
                        transform_x=author_transform_x,
                        transform_y=author_transform_y,
                    ),
                )
                author_count += 1
            except Exception as exc:
                skipped.append({
                    "clip_id": clip.get("clip_id"),
                    "reason": f"author_id_overlay_failed: {exc}",
                    "author": author,
                })
        if author_count:
            added.append({
                "clip_id": "author_id_overlay",
                "type": "author_id_overlay",
                "segments": author_count,
                "track_name": "AuthorID",
            })

    bgm = plan.get("bgm") or {}
    bgm_path_value = str(bgm.get("audio_path") or "")
    if bgm_path_value:
        bgm_path = _resolve_path(bgm_path_value, base_dir)
        if bgm_path.exists():
            bgm_path = _bundle_media(bgm_path, media_dir)
            audio_path = _extract_audio(
                bgm_path,
                media_dir,
                duration_seconds=_seconds(bgm.get("duration_seconds"), 30.0),
            )
            requested_bgm_duration = _seconds(
                bgm.get("duration_seconds"), 30.0
            )
            visual_end = max(
                (
                    _seconds(clip.get("timeline_out"))
                    for clip in plan.get("timeline") or []
                ),
                default=requested_bgm_duration,
            )
            end_guard_seconds = max(
                0.0,
                _seconds(bgm.get("end_guard_seconds"), 0.04),
            )
            bgm_duration = max(
                0.0,
                min(requested_bgm_duration, visual_end) - end_guard_seconds,
            )
            usable_bgm_path = Path(audio_path or bgm_path)
            source_bgm_duration = _probe_duration_seconds(usable_bgm_path)
            if source_bgm_duration <= 0:
                raise RuntimeError("BGM source has no usable duration")
            base_volume = max(0.0, _seconds(bgm.get("base_volume"), 1.0))
            merged_ranges: list[list[float]] = []
            for start, end in sorted(original_audio_ranges):
                start = max(0.0, min(start, bgm_duration))
                end = max(start, min(end, bgm_duration))
                if end <= start:
                    continue
                if merged_ranges and start <= merged_ranges[-1][1] + 0.001:
                    merged_ranges[-1][1] = max(merged_ranges[-1][1], end)
                else:
                    merged_ranges.append([start, end])
            duck_ranges = _voiceover_ranges(plan)
            ducking_db = min(
                0.0,
                _seconds(bgm.get("voiceover_ducking_db"), -7.0),
            )
            duck_volume = base_volume * (10 ** (ducking_db / 20.0))
            boundaries = {0.0, bgm_duration}
            loop_point = source_bgm_duration
            while loop_point < bgm_duration - 0.001:
                boundaries.add(loop_point)
                loop_point += source_bgm_duration
            for start, end in [*merged_ranges, *duck_ranges]:
                boundaries.add(max(0.0, min(float(start), bgm_duration)))
                boundaries.add(max(0.0, min(float(end), bgm_duration)))
            points = sorted(boundaries)
            bgm_ranges: list[tuple[float, float, float, bool]] = []
            for start, end in zip(points, points[1:]):
                if end - start <= 0.01:
                    continue
                middle = (start + end) / 2
                muted = any(left <= middle < right for left, right in merged_ranges)
                if muted:
                    continue
                ducked = any(left <= middle < right for left, right in duck_ranges)
                bgm_ranges.append(
                    (
                        start,
                        end,
                        duck_volume if ducked else base_volume,
                        ducked,
                    )
                )
            try:
                for index, (start, end, volume, ducked) in enumerate(bgm_ranges, start=1):
                    _add_audio_slice(
                        project,
                        usable_bgm_path,
                        target_start=start,
                        source_start=start % source_bgm_duration,
                        duration=end - start,
                        track_name="BGM",
                        volume=volume,
                    )
                added.append({
                    "clip_id": "bgm",
                    "type": "bgm",
                    "segments": len(bgm_ranges),
                    "muted_for_original_audio_ranges": merged_ranges,
                    "ducked_for_voiceover_ranges": duck_ranges,
                    "voiceover_ducking_db": ducking_db,
                    "base_volume": round(base_volume, 4),
                    "ducking_volume": round(duck_volume, 4),
                    "source_duration_seconds": round(source_bgm_duration, 3),
                    "looped": source_bgm_duration < bgm_duration - 0.001,
                    "target_duration_seconds": round(bgm_duration, 3),
                    "visual_end_seconds": round(visual_end, 3),
                    "end_guard_seconds": round(end_guard_seconds, 3),
                })
            except Exception as exc:
                skipped.append({"clip_id": "bgm", "reason": f"add_audio_failed: {exc}", "path": str(bgm_path)})
        else:
            skipped.append({"clip_id": "bgm", "reason": "missing_audio", "path": str(bgm_path)})

    cover = ((plan.get("assets") or {}).get("cover") or {})
    if cover.get("enabled") and cover.get("title"):
        title, _accent_color = _cover_style_from_plan(plan, base_dir)
        cover_font = _cover_font_from_plan(plan, title)
        title_style = cover.get("title_style") or {}
        shadow_style = title_style.get("shadow") or {}
        project.add_text_simple(
            title,
            start_time="0s",
            duration=_time(_seconds(cover.get("duration_seconds"), 3.0) or 3.0),
            track_name="CoverTitle",
            font=cover_font,
            style=draft.TextStyle(
                size=float(title_style.get("font_size", 14.0)),
                bold=True,
                color=_hex_to_rgb01(str(title_style.get("color") or "#FFFFFF")),
                alpha=1.0,
                align=1,
                line_spacing=float(title_style.get("line_spacing", 2.0)),
                auto_wrapping=bool(title_style.get("auto_wrapping", True)),
            ),
            shadow=draft.TextShadow(
                color=_hex_to_rgb01(str(shadow_style.get("color") or "#000000")),
                alpha=float(shadow_style.get("alpha", 0.58)),
                diffuse=float(shadow_style.get("diffuse", 10.0)),
                distance=float(shadow_style.get("distance", 7.0)),
                angle=float(shadow_style.get("angle", -90.0)),
            ),
            clip_settings=draft.ClipSettings(
                transform_y=float(title_style.get("transform_y", -0.72))
            ),
        )

    cta = ((plan.get("assets") or {}).get("cta_voiceover") or {})
    cta_lines = cta.get("lines") or []
    cta_subtitles = cta.get("subtitles") or []
    if not cta_subtitles and cta_lines:
        first_audio_value = str(cta_lines[0].get("audio_path") or "")
        if first_audio_value:
            first_audio_path = _resolve_path(first_audio_value, base_dir)
            automatic_subtitles = first_audio_path.with_name("subtitles.json")
            if automatic_subtitles.exists():
                try:
                    cta_subtitles = (
                        json.loads(
                            automatic_subtitles.read_text(encoding="utf-8")
                        ).get("segments")
                        or []
                    )
                except (OSError, json.JSONDecodeError):
                    cta_subtitles = []
    if cta.get("enabled") and cta_lines:
        voiceover_volume = max(0.0, _seconds(cta.get("volume"), 1.0))
        prepared_lines: list[tuple[str, Path, float]] = []
        for line in cta_lines:
            text = str(line.get("text") or "").strip()
            source_path = _resolve_path(str(line.get("audio_path") or ""), base_dir)
            if not text or not source_path.exists():
                skipped.append({
                    "clip_id": "cta_voiceover",
                    "reason": "missing_text_or_audio",
                    "path": str(source_path),
                })
                continue
            duration = _seconds(line.get("audio_seconds")) or _probe_duration_seconds(source_path)
            if duration <= 0:
                skipped.append({
                    "clip_id": "cta_voiceover",
                    "reason": "invalid_audio_duration",
                    "path": str(source_path),
                })
                continue
            prepared_lines.append((text, _bundle_media(source_path, media_dir), duration))

        if prepared_lines:
            timeline = plan.get("timeline") or []
            openpage_clip = next(
                (
                    clip for clip in timeline
                    if ((clip.get("source") or {}).get("clip_type") == "fixed_openpage")
                ),
                None,
            )
            screen_clip = next(
                (
                    clip for clip in timeline
                    if ((clip.get("source") or {}).get("clip_type") == "fixed_screenshot")
                ),
                None,
            )
            tail_clip = next(
                (
                    clip for clip in timeline
                    if ((clip.get("source") or {}).get("clip_type") == "fixed_tail_sticker")
                ),
                None,
            )
            tail_start = _seconds((tail_clip or {}).get("timeline_in"), _seconds((plan.get("output") or {}).get("duration_seconds")))
            gap_seconds = 0.10
            total_seconds = sum(item[2] for item in prepared_lines) + gap_seconds * max(0, len(prepared_lines) - 1)
            product_start = _seconds(
                (openpage_clip or screen_clip or {}).get("timeline_in"),
                -1.0,
            )
            if product_start >= 0:
                cursor = product_start
                placement = "start_at_product_openpage_allow_creator_overflow"
            else:
                cursor = max(0.0, tail_start - total_seconds)
                placement = "before_tail_fallback"
            for index, (text, audio_path, duration) in enumerate(prepared_lines, start=1):
                audio_seg = project.add_audio_safe(
                    str(audio_path),
                    start_time=_microseconds(cursor),
                    duration=_microseconds(duration),
                    track_name="CTAVoiceover",
                )
                audio_seg.volume = voiceover_volume
                subtitle_items = cta_subtitles if index == 1 and cta_subtitles else [
                    {"text": text, "start": 0.0, "end": duration}
                ]
                subtitle_count = 0
                for subtitle in subtitle_items:
                    subtitle_text = str(subtitle.get("text") or "").strip()
                    relative_start = max(0.0, _seconds(subtitle.get("start")))
                    relative_end = min(
                        duration,
                        _seconds(subtitle.get("end"), duration),
                    )
                    if not subtitle_text or relative_end <= relative_start:
                        continue
                    project.add_text_simple(
                        subtitle_text,
                        start_time=_microseconds(cursor + relative_start),
                        duration=_microseconds(relative_end - relative_start),
                        track_name="CTAText",
                        style=draft.TextStyle(
                            size=7.0,
                            bold=True,
                            color=(1.0, 1.0, 1.0),
                            alpha=1.0,
                            align=1,
                            auto_wrapping=False,
                        ),
                        border=draft.TextBorder(
                            color=(0.0, 0.0, 0.0),
                            alpha=0.9,
                            width=32.0,
                        ),
                        shadow=draft.TextShadow(
                            color=(0.0, 0.0, 0.0),
                            alpha=0.45,
                            diffuse=6.0,
                            distance=4.0,
                            angle=-45.0,
                        ),
                        clip_settings=draft.ClipSettings(transform_y=-0.68),
                    )
                    subtitle_count += 1
                added.append({
                    "clip_id": f"cta_voiceover_{index:02d}",
                    "type": "cta_voiceover",
                    "text": text,
                    "path": str(audio_path),
                    "target_start": cursor,
                    "duration": duration,
                    "audio_added": audio_seg is not None,
                    "volume": round(voiceover_volume, 4),
                    "placement": placement,
                    "subtitle_segments": subtitle_count,
                })
                cursor += duration + gap_seconds

    save_result = project.save()
    return {
        "draft_name": draft_name,
        "draft_path": save_result.get("draft_path"),
        "added_count": len(added),
        "skipped_count": len(skipped),
        "added": added,
        "skipped": skipped,
        "media_dir": str(media_dir),
        "aspect_ratio": aspect_ratio,
        "canvas": {"width": canvas_width, "height": canvas_height},
        "timeline_repairs": timeline_repairs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a Rednote clip plan to a Jianying draft.")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--name", default="rednote_plan_editable_test")
    parser.add_argument("--drafts-root", type=Path, default=DEFAULT_DRAFTS_ROOT)
    parser.add_argument("--media-root", type=Path, default=DEFAULT_MEDIA_ROOT)
    parser.add_argument("--result-output", type=Path)
    parser.add_argument("--no-overwrite", action="store_true")
    args = parser.parse_args()

    result = export_plan(
        args.plan, args.name, args.drafts_root,
        overwrite=not args.no_overwrite, media_root=args.media_root,
    )
    if args.result_output:
        args.result_output.parent.mkdir(parents=True, exist_ok=True)
        args.result_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
