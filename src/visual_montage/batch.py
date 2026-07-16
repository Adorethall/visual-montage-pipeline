from __future__ import annotations

import itertools
import json
from pathlib import Path

from .candidate_registry import CandidateRegistry
from .io import load_campaign, load_yaml, write_json
from .models import TimelineItem, VisualCandidate
from .montage import Slot, build_slots, validate_timeline
from .packaging import fixed_package_items, validate_package
from .scoring import rank_candidates


def _select_for_slots(
    slots: list[Slot],
    candidates: list[VisualCandidate],
    history: dict[str, dict],
    unavailable: set[str],
    source_global_counts: dict[str, int],
    max_per_source: int,
    source_max_global_uses: int,
    previous_batch_penalty: float,
    historical_use_penalty: float,
    opening_sources: set[str],
) -> list[VisualCandidate]:
    chosen: list[VisualCandidate] = []
    local_counts: dict[str, int] = {}
    last_video = ""
    last_event = ""
    for index, slot in enumerate(slots):
        eligible = [
            candidate
            for candidate in candidates
            if candidate.candidate_id not in unavailable
            and not history.get(candidate.candidate_id, {}).get("reserved")
            and local_counts.get(candidate.video_id, 0) < max_per_source
            and source_global_counts.get(candidate.video_id, 0) < source_max_global_uses
            and candidate.preferred_trim.duration >= slot.duration * 0.8
        ]
        if index == 0:
            different_openers = [
                candidate for candidate in eligible if candidate.video_id not in opening_sources
            ]
            if different_openers:
                eligible = different_openers
        if not eligible:
            break

        def adjusted(candidate: VisualCandidate) -> tuple:
            record = history.get(candidate.candidate_id) or {}
            exported = int(record.get("exported_count") or 0)
            previous = 1 if exported else 0
            score = (
                candidate.final_score
                - previous * previous_batch_penalty
                - exported * historical_use_penalty
            )
            return (
                candidate.video_id == last_video,
                candidate.event == last_event,
                -score,
            )

        eligible.sort(key=adjusted)
        candidate = eligible[0]
        chosen.append(candidate)
        unavailable.add(candidate.candidate_id)
        local_counts[candidate.video_id] = local_counts.get(candidate.video_id, 0) + 1
        source_global_counts[candidate.video_id] = (
            source_global_counts.get(candidate.video_id, 0) + 1
        )
        last_video = candidate.video_id
        last_event = candidate.event
    return chosen


def _timeline(slots: list[Slot], chosen: list[VisualCandidate]) -> list[TimelineItem]:
    return [
        TimelineItem(
            timeline_id=f"tl_{index:03d}",
            role="visual_montage",
            source_type="ugc",
            start=slot.start,
            end=slot.end,
            candidate_id=candidate.candidate_id,
        )
        for index, (slot, candidate) in enumerate(zip(slots, chosen), 1)
    ]


def _diversity_report(
    assignments: dict[str, list[VisualCandidate]],
    history_before: dict[str, dict],
) -> dict:
    pairs = []
    for left_id, right_id in itertools.combinations(assignments, 2):
        left = assignments[left_id]
        right = assignments[right_id]
        left_candidates = {item.candidate_id for item in left}
        right_candidates = {item.candidate_id for item in right}
        left_sources = {item.video_id for item in left}
        right_sources = {item.video_id for item in right}
        pairs.append(
            {
                "a": left_id,
                "b": right_id,
                "candidate_overlap_ratio": round(
                    len(left_candidates & right_candidates)
                    / max(1, min(len(left_candidates), len(right_candidates))),
                    4,
                ),
                "source_overlap_ratio": round(
                    len(left_sources & right_sources)
                    / max(1, min(len(left_sources), len(right_sources))),
                    4,
                ),
                "opening_source_same": bool(
                    left and right and left[0].video_id == right[0].video_id
                ),
            }
        )
    all_selected = [item for values in assignments.values() for item in values]
    never_used = [
        item
        for item in all_selected
        if int((history_before.get(item.candidate_id) or {}).get("exported_count") or 0) == 0
    ]
    return {
        "creative_count": len(assignments),
        "selected_candidate_count": len(all_selected),
        "unique_candidate_count": len({item.candidate_id for item in all_selected}),
        "never_used_candidate_ratio": round(
            len(never_used) / max(1, len(all_selected)), 4
        ),
        "creative_pairs": pairs,
    }


def batch_compose(
    *,
    candidate_pool: Path,
    profile_path: Path,
    campaign_path: Path,
    music_analysis_path: Path,
    registry_path: Path,
    output_dir: Path,
    run_id: str,
    count: int,
) -> dict:
    raw = json.loads(candidate_pool.read_text(encoding="utf-8"))
    category = str(raw.get("category") or load_yaml(profile_path)["category_id"])
    parsed = [
        VisualCandidate.model_validate(item)
        for item in raw.get("candidates", raw)
    ]
    profile = load_yaml(profile_path)
    campaign = load_campaign(campaign_path)
    config = profile.get("batch_generation") or {}
    diversity = config.get("diversity") or {}
    music = json.loads(music_analysis_path.read_text(encoding="utf-8"))
    beats = [float(value) for value in music.get("beats") or []]
    slots = build_slots(
        0.0,
        7.2,
        beats,
        target=float(profile["clip_duration"]["preferred"]),
    ) + build_slots(
        12.8,
        17.5,
        beats,
        target=float(profile["clip_duration"]["preferred"]),
    )

    with CandidateRegistry(registry_path) as registry:
        candidates = registry.register(parsed, category, increment_analysis=False)
        ranked = rank_candidates(candidates, profile.get("preferred_events") or {})
        history = registry.history(category)
        unavailable: set[str] = set()
        source_global_counts: dict[str, int] = {}
        opening_sources: set[str] = set()
        assignments: dict[str, list[VisualCandidate]] = {}
        plans = []
        output_dir.mkdir(parents=True, exist_ok=True)

        for index in range(1, count + 1):
            creative_id = f"{category}-{index:03d}"
            chosen = _select_for_slots(
                slots,
                ranked,
                history,
                unavailable,
                source_global_counts,
                int(diversity.get("source_max_per_video", 2)),
                int(diversity.get("source_max_global_uses", 3)),
                float(diversity.get("previous_batch_penalty", 1.0)),
                float(diversity.get("historical_use_penalty", 0.3)),
                opening_sources,
            )
            if chosen:
                opening_sources.add(chosen[0].video_id)
            assignments[creative_id] = chosen
            visual = _timeline(slots, chosen)
            package = fixed_package_items(campaign)
            timeline = sorted(visual + package, key=lambda item: item.start)
            errors = (
                validate_timeline(timeline, campaign.duration_seconds)
                + validate_package(campaign, package)
            )
            if len(chosen) < len(slots):
                errors.append(
                    f"insufficient unique candidates: {len(chosen)}/{len(slots)}"
                )
            plan = {
                "schema_version": "1.0",
                "run_id": run_id,
                "creative_id": creative_id,
                "campaign": campaign.model_dump(),
                "timeline": [item.model_dump() for item in timeline],
                "selected_candidates": [item.model_dump() for item in chosen],
                "validation": {
                    "passed": not errors,
                    "partial": len(chosen) < len(slots),
                    "errors": errors,
                },
            }
            creative_dir = output_dir / "creatives" / creative_id
            plan_path = creative_dir / "compose-plan.json"
            write_json(plan_path, plan)
            plans.append(
                {
                    "creative_id": creative_id,
                    "plan_path": str(plan_path),
                    "candidate_count": len(chosen),
                    "validation": plan["validation"],
                }
            )

        reservation_map = {
            creative_id: [item.candidate_id for item in chosen]
            for creative_id, chosen in assignments.items()
        }
        registry.reserve(run_id, reservation_map)
        report = _diversity_report(assignments, history)
        report.update(
            {
                "run_id": run_id,
                "category": category,
                "registry_path": str(registry_path),
                "reservation_state": "reserved",
                "plans": plans,
            }
        )
        write_json(output_dir / "diversity-report.json", report)
        return report
