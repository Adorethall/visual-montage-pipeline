from __future__ import annotations

from dataclasses import dataclass

from .models import TimelineItem, VisualCandidate


@dataclass(frozen=True)
class Slot:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


def loop_beats(
    beats: list[float], source_duration: float, timeline_end: float
) -> list[float]:
    """Repeat source-relative beats using the same period as looped BGM audio."""
    duration = float(source_duration)
    end = float(timeline_end)
    if duration <= 0 or end <= 0:
        return sorted({round(float(value), 3) for value in beats if 0 <= float(value) <= end})
    source_beats = sorted({
        float(value) for value in beats if 0 <= float(value) < duration
    })
    if not source_beats:
        return []
    output: set[float] = set()
    cycle = 0
    while cycle * duration < end:
        offset = cycle * duration
        for beat in source_beats:
            value = beat + offset
            if value > end:
                break
            output.add(round(value, 3))
        cycle += 1
    return sorted(output)


def build_slots(start: float, end: float, beats: list[float], target: float = 1.4) -> list[Slot]:
    points = sorted({round(start, 3), round(end, 3), *(round(x, 3) for x in beats if start < x < end)})
    slots: list[Slot] = []
    cursor = start
    for point in points[1:]:
        if point - cursor >= target * 0.75:
            slots.append(Slot(round(cursor, 3), round(point, 3)))
            cursor = point
    if end - cursor >= 0.6:
        slots.append(Slot(round(cursor, 3), round(end, 3)))
    elif slots and slots[-1].end < end:
        # Never leave a black gap just because the final beat remainder is
        # shorter than the preferred minimum clip duration. Extend the last
        # slot to the fixed package boundary instead.
        slots[-1] = Slot(slots[-1].start, round(end, 3))
    elif not slots and end > start:
        slots.append(Slot(round(start, 3), round(end, 3)))
    return slots


def assign_candidates(
    slots: list[Slot], candidates: list[VisualCandidate], max_per_source: int = 2
) -> list[TimelineItem]:
    output: list[TimelineItem] = []
    counts: dict[str, int] = {}
    last_video = ""
    last_event = ""
    remaining = candidates[:]
    for index, slot in enumerate(slots, 1):
        eligible = [
            item for item in remaining
            if counts.get(item.video_id, 0) < max_per_source
            and item.video_id != last_video
            and item.preferred_trim.duration >= slot.duration * 0.8
        ]
        if not eligible:
            eligible = [item for item in remaining if counts.get(item.video_id, 0) < max_per_source]
        if not eligible:
            break
        eligible.sort(key=lambda item: (item.event == last_event, -item.final_score))
        chosen = eligible[0]
        remaining.remove(chosen)
        output.append(TimelineItem(
            timeline_id=f"tl_{index:03d}", role="visual_montage", source_type="ugc",
            start=slot.start, end=slot.end, candidate_id=chosen.candidate_id,
        ))
        counts[chosen.video_id] = counts.get(chosen.video_id, 0) + 1
        last_video, last_event = chosen.video_id, chosen.event
    return output


def validate_timeline(items: list[TimelineItem], duration: float, tolerance: float = 0.1) -> list[str]:
    errors: list[str] = []
    ordered = sorted(items, key=lambda item: item.start)
    if ordered and ordered[0].start > 0.001:
        errors.append(f"timeline starts with gap: 0/{ordered[0].start:.3f}")
    for left, right in zip(ordered, ordered[1:]):
        if right.start < left.end - 0.001:
            errors.append(f"overlap: {left.timeline_id}/{right.timeline_id}")
        elif right.start > left.end + 0.001:
            errors.append(
                f"gap: {left.timeline_id}/{right.timeline_id} "
                f"{left.end:.3f}-{right.start:.3f}"
            )
    if ordered and ordered[-1].end > duration + tolerance:
        errors.append("timeline exceeds campaign duration")
    elif ordered and ordered[-1].end < duration - 0.001:
        errors.append(
            f"timeline ends with gap: {ordered[-1].end:.3f}/{duration:.3f}"
        )
    return errors
