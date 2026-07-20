from __future__ import annotations


def marlin_segment_windows(
    duration: float,
    profile: dict,
) -> list[tuple[float, float]]:
    config = profile.get("marlin_recall") or {}
    maximum = float(config.get("maximum_video_duration_seconds", 120.0))
    segment = min(
        maximum,
        float(config.get("segment_duration_seconds", 115.0)),
    )
    overlap = float(config.get("segment_overlap_seconds", 5.0))
    if segment <= 0 or overlap < 0 or overlap >= segment:
        raise ValueError("invalid Marlin segment duration/overlap configuration")
    if duration <= maximum:
        return [(0.0, duration)]
    windows = []
    start = 0.0
    while start < duration:
        end = min(duration, start + segment)
        windows.append((round(start, 3), round(end, 3)))
        if end >= duration:
            break
        start = end - overlap
    return windows


def offset_marlin_result(value, offset: float):
    if isinstance(value, list):
        return [offset_marlin_result(item, offset) for item in value]
    if not isinstance(value, dict):
        return value
    output = {}
    for key, item in value.items():
        if key in {"start", "end", "start_time", "end_time", "timestamp", "time"}:
            if isinstance(item, (int, float)):
                output[key] = round(float(item) + offset, 3)
                continue
        output[key] = offset_marlin_result(item, offset)
    return output
