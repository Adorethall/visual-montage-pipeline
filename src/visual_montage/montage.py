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
    for left, right in zip(ordered, ordered[1:]):
        if right.start < left.end - 0.001:
            errors.append(f"overlap: {left.timeline_id}/{right.timeline_id}")
    if ordered and ordered[-1].end > duration + tolerance:
        errors.append("timeline exceeds campaign duration")
    return errors

