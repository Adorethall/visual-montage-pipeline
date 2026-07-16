from __future__ import annotations

from .models import VisualCandidate


WEIGHTS = {
    "aesthetic": 0.22,
    "category_event_value": 0.20,
    "payoff": 0.15,
    "action_intensity": 0.13,
    "subject_visibility": 0.10,
    "sharpness": 0.08,
    "composition": 0.07,
    "context_independence": 0.05,
}


def score_candidate(candidate: VisualCandidate, event_weights: dict[str, float]) -> float:
    values = candidate.scores.model_dump()
    values["category_event_value"] = event_weights.get(candidate.event, values["category_event_value"])
    base = sum(values[name] * weight for name, weight in WEIGHTS.items())
    penalty = sum(max(0.0, float(value)) for value in candidate.penalties.values())
    return round(max(0.0, min(1.0, base - penalty)), 4)


def rank_candidates(candidates: list[VisualCandidate], event_weights: dict[str, float]) -> list[VisualCandidate]:
    for candidate in candidates:
        candidate.final_score = score_candidate(candidate, event_weights)
    return sorted(candidates, key=lambda item: item.final_score, reverse=True)

