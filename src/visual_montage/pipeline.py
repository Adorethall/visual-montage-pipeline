from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .cover import split_title, validate_cover_title
from .io import load_campaign, load_yaml, write_json
from .models import VisualCandidate
from .montage import assign_candidates, build_slots, loop_beats, validate_timeline
from .music_features import analyze_audio
from .packaging import fixed_package_items, validate_package
from .scoring import rank_candidates


def run_id(category: str) -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S}_{category}"


def analyze_music(music: Path, output: Path) -> dict:
    result = analyze_audio(music, min_duration_seconds=5)
    write_json(output, result)
    return result


def compose_from_candidates(
    *, candidate_pool: Path, profile_path: Path, campaign_path: Path,
    music_analysis_path: Path, output: Path,
) -> dict:
    raw = json.loads(candidate_pool.read_text(encoding="utf-8"))
    candidates = [VisualCandidate.model_validate(item) for item in raw.get("candidates", raw)]
    profile = load_yaml(profile_path)
    campaign = load_campaign(campaign_path)
    ranked = rank_candidates(candidates, profile.get("preferred_events") or {})
    music = json.loads(music_analysis_path.read_text(encoding="utf-8"))
    beats = loop_beats(
        [float(value) for value in music.get("beats") or []],
        float(music.get("duration_seconds") or 0.0),
        17.5,
    )
    first = build_slots(0.0, 7.2, beats, target=float(profile["clip_duration"]["preferred"]))
    second = build_slots(12.8, 17.5, beats, target=float(profile["clip_duration"]["preferred"]))
    visual = assign_candidates(first + second, ranked, max_per_source=2)
    package = fixed_package_items(campaign)
    timeline = sorted(visual + package, key=lambda item: item.start)
    errors = validate_timeline(timeline, campaign.duration_seconds) + validate_package(campaign, package)
    payload = {
        "schema_version": "1.0", "campaign": campaign.model_dump(),
        "timeline": [item.model_dump() for item in timeline],
        "validation": {"passed": not errors, "errors": errors},
    }
    write_json(output, payload)
    return payload


def build_cover_metadata(
    *, title: str, frame_path: Path, video_id: str, timestamp: float, output: Path,
) -> dict:
    errors = validate_cover_title(title)
    payload = {
        "schema_version": "1.0",
        "source": {"video_id": video_id, "timestamp": timestamp, "frame_path": str(frame_path)},
        "title": {"text": title.replace("\n", ""), "lines": split_title(title), "editable": True},
        "jianying": {"background_asset": str(frame_path), "native_text_segment": True},
        "validation": {"passed": not errors, "errors": errors},
    }
    write_json(output, payload)
    return payload


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
