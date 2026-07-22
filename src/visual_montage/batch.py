from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

from .candidate_registry import CandidateRegistry
from .io import load_campaign, load_yaml, write_json
from .models import TimelineItem, VisualCandidate
from .montage import Slot, build_slots, loop_beats, validate_timeline
from .packaging import fixed_package_items, validate_package
from .scoring import rank_candidates


def _author_key(candidate: VisualCandidate) -> str:
    """Return a stable author key from the manifest-style source filename."""
    stem = Path(candidate.video_path).stem
    starred = re.search(r"(@[^*]+)\*", stem)
    if starred:
        return starred.group(1).strip()
    underscored = re.search(r"_(@[^_]+)_", stem)
    if underscored:
        return underscored.group(1).strip()
    return candidate.video_id


def _select_for_slots(
    slots: list[Slot],
    candidates: list[VisualCandidate],
    history: dict[str, dict],
    candidate_global_counts: dict[str, int],
    source_global_counts: dict[str, int],
    max_per_source: int,
    candidate_max_global_uses: int,
    source_max_global_uses: int,
    previous_batch_penalty: float,
    historical_use_penalty: float,
    opening_sources: set[str],
    target_source_videos: int = 0,
    _allowed_sources: set[str] | None = None,
    author_grouping: dict | None = None,
) -> list[VisualCandidate]:
    if target_source_videos > 0 and _allowed_sources is None:
        available_sources = sorted({
            candidate.video_id
            for candidate in candidates
            if candidate_global_counts.get(candidate.candidate_id, 0)
            < candidate_max_global_uses
            and not history.get(candidate.candidate_id, {}).get("reserved")
            and source_global_counts.get(candidate.video_id, 0)
            < source_max_global_uses
        })
        combinations = list(itertools.combinations(
            available_sources, target_source_videos
        ))
        combinations.sort(key=lambda values: (
            sum(source_global_counts.get(value, 0) for value in values),
            max((source_global_counts.get(value, 0) for value in values), default=0),
            -sum(
                1
                for candidate in candidates
                if candidate.video_id in values
                and candidate_global_counts.get(candidate.candidate_id, 0)
                < candidate_max_global_uses
            ),
            values,
        ))
        best: tuple[list[VisualCandidate], dict[str, int], dict[str, int]] | None = None
        for source_group in combinations:
            candidate_counts_copy = dict(candidate_global_counts)
            source_counts_copy = dict(source_global_counts)
            selected = _select_for_slots(
                slots,
                candidates,
                history,
                candidate_counts_copy,
                source_counts_copy,
                max_per_source,
                candidate_max_global_uses,
                source_max_global_uses,
                previous_batch_penalty,
                historical_use_penalty,
                opening_sources,
                target_source_videos,
                set(source_group),
                author_grouping,
            )
            distinct = len({item.video_id for item in selected})
            if best is None or (len(selected), distinct) > (
                len(best[0]), len({item.video_id for item in best[0]})
            ):
                best = (selected, candidate_counts_copy, source_counts_copy)
            if len(selected) == len(slots) and distinct == target_source_videos:
                best = (selected, candidate_counts_copy, source_counts_copy)
                break
        if best is None:
            return []
        candidate_global_counts.clear()
        candidate_global_counts.update(best[1])
        source_global_counts.clear()
        source_global_counts.update(best[2])
        return best[0]

    chosen: list[VisualCandidate] = []
    local_counts: dict[str, int] = {}
    local_candidates: set[str] = set()
    last_video = ""
    last_event = ""
    author_grouping = author_grouping or {}
    group_authors = bool(author_grouping.get("enabled", False))
    avoid_cross_section_reuse = bool(
        author_grouping.get("avoid_cross_section_reuse", False)
    )
    current_author = ""
    current_section = ""
    previous_section_authors: set[str] = set()
    section_authors: set[str] = set()
    for index, slot in enumerate(slots):
        eligible = [
            candidate
            for candidate in candidates
            if (_allowed_sources is None or candidate.video_id in _allowed_sources)
            and candidate.candidate_id not in local_candidates
            and candidate_global_counts.get(candidate.candidate_id, 0)
            < candidate_max_global_uses
            and not history.get(candidate.candidate_id, {}).get("reserved")
            and local_counts.get(candidate.video_id, 0) < max_per_source
            and source_global_counts.get(candidate.video_id, 0) < source_max_global_uses
            and candidate.preferred_trim.duration >= slot.duration * 0.8
        ]
        if target_source_videos > 0:
            selected_sources = set(local_counts)
            if len(selected_sources) >= target_source_videos:
                eligible = [
                    candidate
                    for candidate in eligible
                    if candidate.video_id in selected_sources
                ]
            elif not group_authors:
                new_source_candidates = [
                    candidate
                    for candidate in eligible
                    if candidate.video_id not in selected_sources
                ]
                if new_source_candidates:
                    eligible = new_source_candidates
            else:
                # Preserve author runs while there is still enough timeline left
                # to introduce the requested number of distinct source videos.
                missing_sources = target_source_videos - len(selected_sources)
                remaining_slots = len(slots) - index
                if remaining_slots <= missing_sources:
                    new_source_candidates = [
                        candidate
                        for candidate in eligible
                        if candidate.video_id not in selected_sources
                    ]
                    if new_source_candidates:
                        eligible = new_source_candidates
        if index == 0:
            different_openers = [
                candidate for candidate in eligible if candidate.video_id not in opening_sources
            ]
            if different_openers:
                eligible = different_openers
        if not eligible:
            break

        section = "first_montage" if slot.start < 7.2 else "second_montage"
        if group_authors and section != current_section:
            if current_section:
                previous_section_authors.update(section_authors)
            current_section = section
            current_author = ""
            section_authors = set()
        if group_authors and avoid_cross_section_reuse and previous_section_authors:
            fresh_author_candidates = [
                candidate
                for candidate in eligible
                if _author_key(candidate) not in previous_section_authors
            ]
            if fresh_author_candidates:
                eligible = fresh_author_candidates
        if group_authors and current_author:
            same_author_candidates = [
                candidate
                for candidate in eligible
                if _author_key(candidate) == current_author
            ]
            if same_author_candidates:
                eligible = same_author_candidates

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
                candidate_global_counts.get(candidate.candidate_id, 0),
                source_global_counts.get(candidate.video_id, 0),
                local_counts.get(candidate.video_id, 0),
                abs(candidate.preferred_trim.duration - slot.duration),
                (
                    candidate.video_id == last_video
                    if not group_authors else False
                ),
                candidate.event == last_event,
                -score,
            )

        eligible.sort(key=adjusted)
        candidate = eligible[0]
        chosen.append(candidate)
        local_candidates.add(candidate.candidate_id)
        candidate_global_counts[candidate.candidate_id] = (
            candidate_global_counts.get(candidate.candidate_id, 0) + 1
        )
        local_counts[candidate.video_id] = local_counts.get(candidate.video_id, 0) + 1
        source_global_counts[candidate.video_id] = (
            source_global_counts.get(candidate.video_id, 0) + 1
        )
        last_video = candidate.video_id
        last_event = candidate.event
        if group_authors:
            current_author = _author_key(candidate)
            section_authors.add(current_author)
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
    beats = loop_beats(
        [float(value) for value in music.get("beats") or []],
        float(music.get("duration_seconds") or 0.0),
        17.5,
    )
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
        candidate_global_counts: dict[str, int] = {}
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
                candidate_global_counts,
                source_global_counts,
                int(diversity.get("source_max_per_video", 2)),
                max(1, int(diversity.get("candidate_max_global_uses_per_batch", 1))),
                int(diversity.get("source_max_global_uses", 3)),
                float(diversity.get("previous_batch_penalty", 1.0)),
                float(diversity.get("historical_use_penalty", 0.3)),
                opening_sources,
                max(0, int(diversity.get("source_videos_per_creative", 0))),
                author_grouping=dict(config.get("author_grouping") or {}),
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
            target_sources = max(
                0, int(diversity.get("source_videos_per_creative", 0))
            )
            selected_source_count = len({item.video_id for item in chosen})
            if target_sources and selected_source_count != target_sources:
                errors.append(
                    "insufficient distinct source videos: "
                    f"{selected_source_count}/{target_sources}"
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
                "candidate_usage_counts": candidate_global_counts,
                "maximum_candidate_use_count": max(
                    candidate_global_counts.values(), default=0
                ),
                "plans": plans,
            }
        )
        write_json(output_dir / "diversity-report.json", report)
        return report
