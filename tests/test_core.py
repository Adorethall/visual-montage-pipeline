from visual_montage.cover import split_title, validate_cover_title
from visual_montage.models import CandidateScores, TimeRange, VisualCandidate
from visual_montage.montage import assign_candidates, build_slots, validate_timeline
from visual_montage.scoring import rank_candidates


def candidate(index: int, video: str, event: str, score: float) -> VisualCandidate:
    return VisualCandidate(
        candidate_id=f"c{index}", video_id=video, video_path=f"/{video}.mp4", event=event,
        source_window=TimeRange(start=index * 3, end=index * 3 + 3),
        preferred_trim=TimeRange(start=index * 3, end=index * 3 + 2),
        scores=CandidateScores(aesthetic=score, payoff=score, action_intensity=score, subject_visibility=1),
    )


def test_scoring_and_diverse_assignment() -> None:
    items = [candidate(1, "a", "result", .9), candidate(2, "a", "detail", .8), candidate(3, "b", "result", .7)]
    ranked = rank_candidates(items, {"result": 1, "detail": .7})
    timeline = assign_candidates(build_slots(0, 3, [1.5]), ranked)
    assert len(timeline) == 2
    assert timeline[0].candidate_id != timeline[1].candidate_id
    assert validate_timeline(timeline, 3) == []


def test_cover_title_contract() -> None:
    assert validate_cover_title("今天想换哪种妆感") == []
    assert 1 <= len(split_title("今天想换哪种妆感")) <= 2
    assert validate_cover_title("精彩内容")
