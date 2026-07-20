from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .batch import batch_compose
from .candidate_registry import CandidateRegistry
from .io import load_yaml, write_json
from .voiceover import generate_voiceover
from .audio_levels import adaptive_mix_levels
from .subtitles import generate_subtitles


def _author_from_video_path(video_path: str) -> str:
    """Extract the manifest-style @author token from a material filename."""
    stem = Path(video_path).stem
    starred = re.search(r"(@[^*]+)\*", stem)
    if starred:
        return starred.group(1).strip()
    marker = stem.find("@")
    if marker < 0:
        return ""
    tail = re.sub(r"_[0-9a-fA-F]{24}$", "", stem[marker:])
    parts = tail.rsplit("_", 2)
    return parts[0].strip() if parts else ""


def _logo_aligned_transform_y(logo_path: Path, fallback: float = 0.82) -> float:
    """Align text center with the visible (non-transparent) logo center."""
    try:
        with Image.open(logo_path) as image:
            rgba = image.convert("RGBA")
            bounds = rgba.getchannel("A").getbbox()
            if not bounds or rgba.height <= 0:
                return fallback
            center_y = (bounds[1] + bounds[3]) / 2.0
            return round(max(-1.0, min(1.0, 1.0 - 2.0 * center_y / rgba.height)), 6)
    except OSError:
        return fallback


def _asset_index(library: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output = {}
    for section in (
        "product_openpages",
        "product_recordings",
        "brand_assets",
        "overlay_assets",
        "fonts",
    ):
        for item in library.get(section) or []:
            output[str(item["asset_id"])] = item
    return output


def _asset_path(
    asset_id: str,
    index: dict[str, dict[str, Any]],
    project_root: Path,
) -> Path:
    item = index.get(asset_id)
    if not item:
        raise KeyError(f"asset is not registered: {asset_id}")
    path = Path(str(item["path"])).expanduser()
    if not path.is_absolute():
        path = project_root / path
    if not path.exists():
        raise FileNotFoundError(path)
    return path.resolve()


def _extract_cover(candidate: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    timestamp = float(candidate.get("peak_time") or candidate["preferred_trim"]["start"])
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{timestamp:.3f}",
            "-i", str(candidate["video_path"]),
            "-frames:v", "1",
            "-vf",
            (
                "scale=720:1280:force_original_aspect_ratio=increase,"
                "crop=720:1280"
            ),
            str(output),
        ],
        check=True,
    )


def _cover_title(profile: dict, index: int, fallback: str) -> str:
    titles = ((profile.get("batch_generation") or {}).get("cover_titles") or [])
    if titles:
        return str(titles[(index - 1) % len(titles)])
    return fallback


def _cover_title_style(cover_raw: dict) -> dict:
    configured = cover_raw.get("title_style") or {}
    shadow = configured.get("shadow") or {}
    return {
        "font_size": float(configured.get("font_size", 14.0)),
        "color": str(configured.get("color") or "#FFFFFF"),
        "align": str(configured.get("align") or "center"),
        "auto_wrapping": bool(configured.get("auto_wrapping", True)),
        "line_spacing": float(configured.get("line_spacing", 2.0)),
        "border_enabled": bool(configured.get("border_enabled", False)),
        "transform_y": float(configured.get("transform_y", -0.50)),
        "preview_center_y_ratio": float(configured.get("preview_center_y_ratio", 0.23)),
        "shadow": {
            "color": str(shadow.get("color") or "#000000"),
            "alpha": float(shadow.get("alpha", 0.58)),
            "diffuse": float(shadow.get("diffuse", 10.0)),
            "distance": float(shadow.get("distance", 7.0)),
            "angle": float(shadow.get("angle", -90.0)),
        },
    }


def _wrap_cover_title(draw, title: str, font, max_width: int, auto_wrapping: bool) -> list[str]:
    paragraphs = str(title).replace("|", "\n").splitlines() or [""]
    if not auto_wrapping:
        return paragraphs
    lines: list[str] = []
    for paragraph in paragraphs:
        tokens = paragraph.split() if " " in paragraph.strip() else list(paragraph)
        separator = " " if " " in paragraph.strip() else ""
        current = ""
        for token in tokens:
            candidate = token if not current else f"{current}{separator}{token}"
            width = draw.textbbox((0, 0), candidate, font=font)[2]
            if current and width > max_width:
                lines.append(current)
                current = token
            else:
                current = candidate
        lines.append(current)
    return lines


def _cover_preview(
    clean_frame: Path,
    output: Path,
    title: str,
    font_path: Path | None,
    logo_path: Path,
    title_style: dict,
) -> None:
    with Image.open(clean_frame) as source:
        canvas = source.convert("RGBA")
    with Image.open(logo_path) as logo:
        logo_rgba = logo.convert("RGBA").resize(canvas.size)
        canvas.alpha_composite(logo_rgba)
    font_size = max(12, round(float(title_style["font_size"]) * 64 / 14))
    try:
        font = ImageFont.truetype(str(font_path), font_size) if font_path else ImageFont.load_default(size=font_size)
    except OSError:
        font = ImageFont.load_default(size=font_size)
    draw = ImageDraw.Draw(canvas)
    max_width = canvas.width - 100
    lines = _wrap_cover_title(
        draw, title, font, max_width, bool(title_style["auto_wrapping"])
    )
    line_height = font_size + round(float(title_style["line_spacing"]) * 9)
    y = int(canvas.height * float(title_style["preview_center_y_ratio"])) - line_height * len(lines) // 2
    shadow = title_style["shadow"]
    shadow_offset = max(2, round(float(shadow["distance"]) * 0.8))
    shadow_alpha = max(0, min(255, round(float(shadow["alpha"]) * 255)))
    placements: list[tuple[float, float, str]] = []
    for line in lines:
        bounds = draw.textbbox((0, 0), line, font=font)
        width = bounds[2] - bounds[0]
        x = (canvas.width - width) / 2
        placements.append((x, y, line))
        y += line_height
    shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    for x, line_y, line in placements:
        shadow_draw.text(
            (x + shadow_offset, line_y + shadow_offset),
            line,
            font=font,
            fill=(0, 0, 0, shadow_alpha),
        )
    shadow_layer = shadow_layer.filter(
        ImageFilter.GaussianBlur(max(1.0, float(shadow["diffuse"]) * 0.45))
    )
    canvas.alpha_composite(shadow_layer)
    draw = ImageDraw.Draw(canvas)
    for x, line_y, line in placements:
        draw.text(
            (x, line_y),
            line,
            font=font,
            fill=title_style["color"],
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output, "JPEG", quality=90, optimize=True)


def build_jianying_plan(
    *,
    compose_plan: dict,
    profile: dict,
    campaign_raw: dict,
    music_analysis: dict,
    asset_library: Path,
    voiceover_audio: Path | None,
    creative_index: int,
    creative_dir: Path,
    project_root: Path,
) -> tuple[dict, dict]:
    library = load_yaml(asset_library)
    assets_by_id = _asset_index(library)
    campaign = compose_plan["campaign"]
    candidates = {
        item["candidate_id"]: item
        for item in compose_plan.get("selected_candidates") or []
    }
    timeline = []
    for item in compose_plan.get("timeline") or []:
        start = float(item["start"])
        end = float(item["end"])
        duration = end - start
        if item["source_type"] == "ugc":
            candidate = candidates[item["candidate_id"]]
            trim = candidate["preferred_trim"]
            source_in = float(trim["start"])
            available = float(trim["end"]) - source_in
            if available < duration:
                source_in = max(0.0, float(trim["end"]) - duration)
            source = {
                "clip_type": "highlight",
                "video_path": candidate["video_path"],
                "author": _author_from_video_path(candidate["video_path"]),
                "source_in": source_in,
                "source_out": source_in + duration,
                "audio_recommendation": "bgm",
                "video_id": candidate["video_id"],
            }
        else:
            asset_id = str(item["asset_id"])
            source = {
                "clip_type": {
                    "product_openpage": "fixed_openpage",
                    "product_recording": "fixed_screenshot",
                    "endcard": "fixed_tail_sticker",
                }[item["source_type"]],
                "video_path": str(_asset_path(asset_id, assets_by_id, project_root)),
                "source_in": 0.0,
                "source_out": duration,
                "audio_recommendation": "bgm",
                "asset_id": asset_id,
            }
        timeline.append(
            {
                "clip_id": item["timeline_id"],
                "timeline_in": start,
                "timeline_out": end,
                "duration": duration,
                "source": source,
            }
        )

    selected = compose_plan.get("selected_candidates") or []
    if not selected:
        raise ValueError("compose plan has no selected candidates")
    cover_clean = creative_dir / "cover" / "cover-clean.jpg"
    _extract_cover(selected[0], cover_clean)
    cover_title = _cover_title(
        profile,
        creative_index,
        str((campaign_raw.get("copy") or {}).get("hook") or "今日妆容灵感"),
    )
    brand = campaign_raw.get("brand") or {}
    cover_raw = campaign_raw.get("cover") or {}
    cover_title_style = _cover_title_style(cover_raw)
    font_id = str(cover_raw.get("font_asset_id") or "")
    font_path = _asset_path(font_id, assets_by_id, project_root) if font_id else None
    cover_logo_path = _asset_path(
        campaign["cover_logo_asset_id"],
        assets_by_id,
        project_root,
    )
    logo_path = _asset_path(
        campaign["logo_asset_id"],
        assets_by_id,
        project_root,
    )
    cover_preview = creative_dir / "cover" / "cover-preview.jpg"
    _cover_preview(
        cover_clean,
        cover_preview,
        cover_title,
        font_path,
        cover_logo_path,
        cover_title_style,
    )
    voice_text = str((campaign_raw.get("voiceover") or {}).get("text") or "")
    audio_duration = _probe_duration(voiceover_audio)
    subtitles_path = voiceover_audio.with_name("subtitles.json")
    generate_subtitles(
        audio_path=voiceover_audio,
        source_text=voice_text,
        output=subtitles_path,
        cache_dir=project_root / "data" / "cache" / "voiceover" / "subtitles",
    )
    subtitles = json.loads(subtitles_path.read_text(encoding="utf-8"))
    music_path = Path(str(
        music_analysis.get("selected_audio_path")
        or music_analysis.get("audio_path")
        or music_analysis.get("source_path")
    )).expanduser()
    if not music_path.is_absolute():
        music_path = project_root / music_path
    if not music_path.exists():
        raise FileNotFoundError(music_path)
    audio_raw = campaign_raw.get("audio") or {}
    mix_levels = adaptive_mix_levels(
        voiceover_audio,
        music_path,
        voice_target_mean_db=float(
            audio_raw.get("voice_target_mean_db", -16)
        ),
        voice_peak_ceiling_db=float(
            audio_raw.get("voice_peak_ceiling_db", -1)
        ),
        bgm_target_mean_db=float(
            audio_raw.get("bgm_target_mean_db", -20)
        ),
        voiceover_margin_db=float(
            audio_raw.get("voiceover_margin_db", 12)
        ),
    )
    plan = {
        "schema_version": "1.0",
        "output": {
            "duration_seconds": float(campaign["duration_seconds"]),
            "aspect_ratio": campaign["aspect_ratio"],
        },
        "timeline": timeline,
        "bgm": {
            "audio_path": str(music_path.resolve()),
            "duration_seconds": float(campaign["duration_seconds"]),
            "end_guard_seconds": float(
                audio_raw.get("bgm_end_guard_seconds", 0.04)
            ),
            "base_volume": mix_levels["bgm"]["volume"],
            "voiceover_ducking_db": mix_levels["bgm"][
                "voiceover_ducking_db"
            ],
            "mix_analysis": mix_levels["bgm"],
        },
        "assets": {
            "asset_library_path": str(asset_library.resolve()),
            "endcard_freeze_last_seconds": float(
                brand.get("endcard_freeze_last_seconds", 0.0)
            ),
            "logo_overlay": {
                "enabled": True,
                "asset_id": campaign["logo_asset_id"],
                "end_guard_seconds": float(
                    brand.get("logo_end_guard_seconds", 0.0)
                ),
                "image_path": str(
                    _asset_path(campaign["logo_asset_id"], assets_by_id, project_root)
                ),
                "hide_on_clip_types": [
                    "fixed_openpage",
                    "fixed_screenshot",
                    "fixed_tail_sticker",
                ],
            },
            "logo_intro_overlay": {
                "enabled": True,
                "asset_id": campaign["animated_logo_asset_id"],
                "video_path": str(
                    _asset_path(
                        campaign["animated_logo_asset_id"],
                        assets_by_id,
                        project_root,
                    )
                ),
            },
            "cover_logo_overlay": {
                "enabled": True,
                "asset_id": campaign["cover_logo_asset_id"],
                "image_path": str(
                    cover_logo_path
                ),
            },
            "cover": {
                "enabled": True,
                "title": cover_title,
                "duration_seconds": 0.1,
                "frame_path": str(cover_clean),
                "font_path": str(font_path) if font_path else "",
                "language": campaign.get("language", "zh-CN"),
                "title_style": cover_title_style,
            },
            "author_id_overlay": {
                "enabled": True,
                "position": "top_right",
                "font_size": 4.8,
                "emoji_font_scale": 1.0,
                "font_path": str(font_path) if font_path else "",
                "right_edge_transform_x": 0.92,
                "draft_transform_y": round(
                    min(1.0, _logo_aligned_transform_y(logo_path) + 0.05),
                    6,
                ),
                "vertical_alignment": "one_line_above_visible_logo_center",
                "vertical_offset": 0.05,
            },
            "cta_voiceover": {
                "enabled": True,
                "volume": mix_levels["voiceover"]["volume"],
                "mix_analysis": mix_levels["voiceover"],
                "subtitles_path": str(subtitles_path.resolve()),
                "subtitles": subtitles.get("segments") or [],
                "lines": [
                    {
                        "text": voice_text,
                        "audio_path": str(voiceover_audio.resolve()),
                        "audio_seconds": audio_duration,
                    }
                ],
            },
        },
    }
    cover_metadata = {
        "title": cover_title,
        "editable": True,
        "clean_frame": str(cover_clean),
        "preview_frame": str(cover_preview),
        "source_video_id": selected[0]["video_id"],
        "source_timestamp": selected[0].get("peak_time"),
    }
    return plan, cover_metadata


def _probe_duration(path: Path) -> float:
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


def run_batch(
    *,
    project_root: Path,
    manifest: Path,
    category: str,
    limit: int,
    count: int,
    campaign_path: Path,
    profile_path: Path,
    music_analysis_path: Path | None,
    voiceover_audio: Path | None,
    asset_library: Path,
    registry_path: Path,
    run_id: str,
    env_file: Path,
    drafts_root: Path,
    media_root: Path,
    force_analysis: bool = False,
    force_audio: bool = False,
    cache_only: bool = False,
    voiceover_mode: str = "cached",
) -> dict:
    run_dir = project_root / "data" / "runs" / run_id
    analysis_dir = run_dir / "analysis"
    if voiceover_audio is None:
        voiceover_audio = run_dir / "voiceover" / "product-sequence.wav"
        generate_voiceover(
            campaign_path=campaign_path,
            output=voiceover_audio,
            cache_dir=project_root / "data" / "cache" / "voiceover",
            force=voiceover_mode == "regenerate",
        )
    elif not voiceover_audio.exists():
        raise FileNotFoundError(voiceover_audio)
    analyze_command = [
        sys.executable,
        str(project_root / "scripts" / "analyze_visual_batch.py"),
        "--manifest", str(manifest),
        "--category", category,
        "--limit", str(limit),
        "--output-dir", str(analysis_dir),
        "--env-file", str(env_file),
        "--profile", str(profile_path),
    ]
    if force_analysis:
        analyze_command.append("--force")
    if force_audio:
        analyze_command.append("--force-audio")
    if cache_only:
        analyze_command.append("--cache-only")
    subprocess.run(analyze_command, check=True, cwd=project_root)
    candidate_pool_path = analysis_dir / "candidate-pool.json"
    candidate_pool = json.loads(candidate_pool_path.read_text(encoding="utf-8"))
    analysis_failures = candidate_pool.get("failures") or []

    profile = load_yaml(profile_path)
    audio_config = profile.get("audio_bgm_analysis") or {}
    selected_bgms = []
    manual_music = None
    minimum_bgm_duration = float(
        audio_config.get("minimum_bgm_duration_seconds", 5.0)
    )
    if music_analysis_path:
        manual_music = json.loads(music_analysis_path.read_text(encoding="utf-8"))
        manual_duration = float(
            manual_music.get("duration_seconds")
            or (
                float((manual_music.get("best_window") or {}).get("end", 0))
                - float((manual_music.get("best_window") or {}).get("start", 0))
            )
        )
        if manual_duration < minimum_bgm_duration:
            raise RuntimeError(
                f"BGM is too short: {manual_duration:.3f}s; "
                f"minimum is {minimum_bgm_duration:.3f}s"
            )
        selected_bgms = [manual_music for _ in range(count)]
    else:
        with CandidateRegistry(registry_path) as registry:
            selected_bgms = registry.select_bgms(
                category,
                count,
                minimum_score=float(audio_config.get("minimum_music_score", 0.68)),
                maximum_speech_risk=float(audio_config.get("maximum_speech_risk", 0.18)),
                same_bgm_max_per_batch=int(
                    audio_config.get("same_bgm_max_per_batch", 2)
                ),
                target_bpm=float(
                    (audio_config.get("preferred_bpm") or {}).get("target", 122)
                ),
                minimum_duration_seconds=minimum_bgm_duration,
            )
    if not selected_bgms:
        raise RuntimeError(
            "no eligible BGM found; provide --music-analysis or analyze more videos"
        )
    compose_music = selected_bgms[0]
    compose_music_path = run_dir / "selected-bgm-analysis.json"
    write_json(
        compose_music_path,
        {
            **compose_music,
            "audio_path": compose_music.get("selected_audio_path")
            or compose_music.get("audio_path"),
        },
    )
    report = batch_compose(
        candidate_pool=candidate_pool_path,
        profile_path=profile_path,
        campaign_path=campaign_path,
        music_analysis_path=compose_music_path,
        registry_path=registry_path,
        output_dir=run_dir,
        run_id=run_id,
        count=count,
    )
    campaign_raw = load_yaml(campaign_path)
    results = []
    any_failure = False
    with CandidateRegistry(registry_path) as registry:
        for creative_index, item in enumerate(report["plans"], 1):
            creative_id = item["creative_id"]
            creative_dir = run_dir / "creatives" / creative_id
            music = selected_bgms[(creative_index - 1) % len(selected_bgms)]
            bgm_id = str(music.get("bgm_id") or "")
            if not item["validation"]["passed"]:
                registry.finalize_creative(run_id, creative_id, "released")
                results.append({
                    "creative_id": creative_id,
                    "status": "partial",
                    "errors": item["validation"]["errors"],
                })
                continue
            try:
                if bgm_id:
                    registry.reserve_bgm(bgm_id, run_id, creative_id)
                compose_plan = json.loads(Path(item["plan_path"]).read_text(encoding="utf-8"))
                plan, cover = build_jianying_plan(
                    compose_plan=compose_plan,
                    profile=profile,
                    campaign_raw=campaign_raw,
                    music_analysis=music,
                    asset_library=asset_library,
                    voiceover_audio=voiceover_audio,
                    creative_index=creative_index,
                    creative_dir=creative_dir,
                    project_root=project_root,
                )
                plan_path = creative_dir / "jianying-plan.json"
                write_json(plan_path, plan)
                write_json(creative_dir / "cover" / "cover.json", cover)
                result_path = creative_dir / "jianying-result.json"
                draft_name = f"{run_id}-{creative_index:03d}"
                subprocess.run(
                    [
                        sys.executable,
                        str(project_root / "scripts" / "export_jianying_plan.py"),
                        "--plan", str(plan_path),
                        "--name", draft_name,
                        "--drafts-root", str(drafts_root),
                        "--media-root", str(media_root),
                        "--result-output", str(result_path),
                    ],
                    check=True,
                    cwd=project_root,
                )
                export_result = json.loads(result_path.read_text(encoding="utf-8"))
                if export_result.get("skipped_count") or not Path(
                    str(export_result.get("draft_path") or "")
                ).exists():
                    raise RuntimeError("JianYing draft validation failed")
                registry.finalize_creative(run_id, creative_id, "committed")
                if bgm_id:
                    registry.finalize_bgm(
                        bgm_id, run_id, creative_id, "committed"
                    )
                results.append({
                    "creative_id": creative_id,
                    "status": "committed",
                    "draft_name": draft_name,
                    "draft_path": export_result["draft_path"],
                    "plan_path": str(plan_path),
                    "cover": cover,
                    "bgm_id": bgm_id or "manual",
                    "bgm_path": str(
                        music.get("selected_audio_path") or music.get("audio_path")
                    ),
                })
            except Exception as exc:
                any_failure = True
                registry.finalize_creative(run_id, creative_id, "released")
                if bgm_id:
                    registry.finalize_bgm(
                        bgm_id, run_id, creative_id, "released"
                    )
                results.append({
                    "creative_id": creative_id,
                    "status": "failed",
                    "error": str(exc),
                })

    payload = {
        "ok": not any_failure and any(
            item["status"] == "committed" for item in results
        ),
        "run_id": run_id,
        "category": category,
        "requested_count": count,
        "committed_count": sum(item["status"] == "committed" for item in results),
        "partial_count": sum(item["status"] == "partial" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "analysis": str(candidate_pool_path),
        "analysis_failures": analysis_failures,
        "warnings": [
            (
                f"{len(analysis_failures)} source video(s) failed analysis; "
                "the batch continued with successful candidates"
            )
        ] if analysis_failures else [],
        "diversity_report": str(run_dir / "diversity-report.json"),
        "creatives": results,
    }
    write_json(run_dir / "result.json", payload)
    return payload
