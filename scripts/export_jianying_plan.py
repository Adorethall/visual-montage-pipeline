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

    for clip in plan.get("timeline") or []:
        source = clip.get("source") or {}
        media_path = _resolve_path(str(source.get("video_path") or ""), base_dir)
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
        added.append(
            {
                "clip_id": clip.get("clip_id"),
                "type": source.get("clip_type"),
                "path": str(media_path),
                "target_start": clip.get("timeline_in"),
                "source_start": source.get("source_in"),
                "duration": duration,
            }
        )

    assets = plan.get("assets") or {}
    logo = assets.get("logo_overlay") or {}
    intro_logo, cover_logo = _infer_logo_sequence(plan, base_dir, logo)
    cover = assets.get("cover") or {}
    cover_duration = (
        _seconds(cover.get("duration_seconds"))
        if cover.get("enabled")
        else 0.0
    )
    intro_enabled = bool(intro_logo.get("enabled"))
    cover_logo_enabled = bool(cover_logo.get("enabled"))
    if logo.get("enabled"):
        logo_path = _overlay_asset_path(plan, logo, base_dir, "image_path")
        hidden_types = set(logo.get("hide_on_clip_types") or [])
        if logo_path:
            logo_path = _bundle_media(logo_path, media_dir)
            logo_count = 0
            eligible = _eligible_logo_clips(plan, hidden_types)
            first_clip = eligible[0] if eligible else None
            for clip in eligible:
                duration = _seconds(clip.get("duration"))
                start = _seconds(clip.get("timeline_in"))
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
    author_font_size = float(author_overlay.get("font_size") or 7.0)
    author_transform_x = float(author_overlay.get("draft_transform_x") or 0.62)
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
            try:
                project.add_text_simple(
                    author,
                    start_time=_microseconds(clip.get("timeline_in")),
                    duration=_microseconds(duration),
                    track_name="AuthorID",
                    style=draft.TextStyle(
                        size=author_font_size,
                        bold=True,
                        color=(1.0, 1.0, 1.0),
                        alpha=1.0,
                        align=0,
                        auto_wrapping=False,
                    ),
                    border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.9, width=32.0),
                    shadow=draft.TextShadow(
                        color=(0.0, 0.0, 0.0),
                        alpha=0.45,
                        diffuse=6.0,
                        distance=4.0,
                        angle=-45.0,
                    ),
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
            bgm_duration = _seconds(bgm.get("duration_seconds"), 30.0)
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
            bgm_ranges: list[tuple[float, float]] = []
            cursor = 0.0
            for start, end in merged_ranges:
                if start > cursor:
                    bgm_ranges.append((cursor, start))
                cursor = max(cursor, end)
            if cursor < bgm_duration:
                bgm_ranges.append((cursor, bgm_duration))
            try:
                for index, (start, end) in enumerate(bgm_ranges, start=1):
                    if end - start <= 0.01:
                        continue
                    project.add_audio_safe(
                        str(audio_path or bgm_path),
                        start_time=_microseconds(start),
                        duration=_microseconds(end - start),
                        track_name="BGM",
                    )
                added.append({
                    "clip_id": "bgm",
                    "type": "bgm",
                    "segments": len(bgm_ranges),
                    "muted_for_original_audio_ranges": merged_ranges,
                })
            except Exception as exc:
                skipped.append({"clip_id": "bgm", "reason": f"add_audio_failed: {exc}", "path": str(bgm_path)})
        else:
            skipped.append({"clip_id": "bgm", "reason": "missing_audio", "path": str(bgm_path)})

    cover = ((plan.get("assets") or {}).get("cover") or {})
    if cover.get("enabled") and cover.get("title"):
        title, accent_color = _cover_style_from_plan(plan, base_dir)
        cover_font = _cover_font_from_plan(plan, title)
        project.add_text_simple(
            title,
            start_time="0s",
            duration=_time(_seconds(cover.get("duration_seconds"), 3.0) or 3.0),
            track_name="CoverTitle",
            font=cover_font,
            style=draft.TextStyle(
                size=14.0,
                bold=True,
                color=accent_color,
                alpha=1.0,
                align=1,
                line_spacing=2,
                auto_wrapping=False,
            ),
            border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.72, width=48.0),
            shadow=draft.TextShadow(
                color=(1.0, 1.0, 1.0),
                alpha=0.2,
                diffuse=10.0,
                distance=7.0,
                angle=-90.0,
            ),
            clip_settings=draft.ClipSettings(transform_y=-0.50),
        )

    cta = ((plan.get("assets") or {}).get("cta_voiceover") or {})
    cta_lines = cta.get("lines") or []
    if cta.get("enabled") and cta_lines:
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
                project.add_text_simple(
                    text,
                    start_time=_microseconds(cursor),
                    duration=_microseconds(duration),
                    track_name="CTAText",
                    style=draft.TextStyle(
                        size=7.0,
                        bold=True,
                        color=(1.0, 1.0, 1.0),
                        alpha=1.0,
                        align=1,
                        auto_wrapping=False,
                    ),
                    border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.9, width=32.0),
                    shadow=draft.TextShadow(
                        color=(0.0, 0.0, 0.0), alpha=0.45, diffuse=6.0,
                        distance=4.0, angle=-45.0,
                    ),
                    clip_settings=draft.ClipSettings(transform_y=-0.68),
                )
                added.append({
                    "clip_id": f"cta_voiceover_{index:02d}",
                    "type": "cta_voiceover",
                    "text": text,
                    "path": str(audio_path),
                    "target_start": cursor,
                    "duration": duration,
                    "audio_added": audio_seg is not None,
                    "placement": placement,
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
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a Rednote clip plan to a Jianying draft.")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--name", default="rednote_plan_editable_test")
    parser.add_argument("--drafts-root", type=Path, default=DEFAULT_DRAFTS_ROOT)
    parser.add_argument("--media-root", type=Path, default=DEFAULT_MEDIA_ROOT)
    parser.add_argument("--no-overwrite", action="store_true")
    args = parser.parse_args()

    result = export_plan(
        args.plan, args.name, args.drafts_root,
        overwrite=not args.no_overwrite, media_root=args.media_root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
