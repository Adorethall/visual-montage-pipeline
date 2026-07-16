import json
from pathlib import Path

from visual_montage.batch import batch_compose
from visual_montage.candidate_registry import CandidateRegistry, stable_candidate_id
from visual_montage.models import CandidateScores, TimeRange, VisualCandidate


def candidate(index: int, video: str) -> VisualCandidate:
    return VisualCandidate(
        candidate_id=f"raw_{index}",
        video_id=video,
        video_path=f"/{video}.mp4",
        event="result" if index % 2 else "detail",
        source_window=TimeRange(start=index * 2, end=index * 2 + 2),
        preferred_trim=TimeRange(start=index * 2, end=index * 2 + 2),
        scores=CandidateScores(
            aesthetic=0.9,
            category_event_value=0.9,
            payoff=0.9,
            action_intensity=0.8,
            subject_visibility=1.0,
        ),
    )


def test_registry_stable_id_and_finalize(tmp_path: Path) -> None:
    item = candidate(1, "video_a")
    assert stable_candidate_id(item, "beauty") == stable_candidate_id(item, "beauty")
    registry_path = tmp_path / "registry.sqlite"
    with CandidateRegistry(registry_path) as registry:
        registered = registry.register([item], "beauty")
        stable_id = registered[0].candidate_id
        registry.reserve("run-1", {"beauty-001": [stable_id]})
        assert registry.history("beauty")[stable_id]["reserved"] == 1
        assert registry.finalize_run("run-1", "committed") == 1
        history = registry.history("beauty")[stable_id]
        assert history["exported_count"] == 1
        assert history["reserved"] == 0


def test_batch_compose_avoids_previous_batch(tmp_path: Path) -> None:
    candidates = [
        candidate(index, f"video_{index % 8}")
        for index in range(1, 25)
    ]
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps({
        "category": "beauty",
        "candidates": [item.model_dump() for item in candidates],
    }))
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        """
category_id: beauty
preferred_events: {result: 1.0, detail: 0.9}
clip_duration: {preferred: 1.4}
batch_generation:
  diversity:
    source_max_per_video: 2
    source_max_global_uses: 4
    previous_batch_penalty: 1.0
    historical_use_penalty: 0.3
"""
    )
    campaign = tmp_path / "campaign.yaml"
    campaign.write_text(
        """
campaign_id: test
category: beauty
duration_seconds: 20
product_demo: {openpage_asset_id: open, recording_asset_id: recording}
brand: {endcard_asset_id: end}
copy: {brand_message: message, cta: go}
voiceover: {text: 第一句。第二句。}
"""
    )
    music = tmp_path / "music.json"
    music.write_text(json.dumps({"beats": [1.4, 2.8, 4.2, 5.6, 13.8, 15.2, 16.6]}))
    registry = tmp_path / "registry.sqlite"

    first = batch_compose(
        candidate_pool=pool,
        profile_path=profile,
        campaign_path=campaign,
        music_analysis_path=music,
        registry_path=registry,
        output_dir=tmp_path / "run-1",
        run_id="run-1",
        count=2,
    )
    with CandidateRegistry(registry) as store:
        store.finalize_run("run-1", "committed")
    second = batch_compose(
        candidate_pool=pool,
        profile_path=profile,
        campaign_path=campaign,
        music_analysis_path=music,
        registry_path=registry,
        output_dir=tmp_path / "run-2",
        run_id="run-2",
        count=2,
    )
    with CandidateRegistry(registry) as store:
        first_ids = {
            item for values in store.usage_for_run("run-1").values() for item in values
        }
        second_ids = {
            item for values in store.usage_for_run("run-2").values() for item in values
        }
    assert first["unique_candidate_count"] > 0
    assert second["unique_candidate_count"] > 0
    assert len(first_ids & second_ids) < len(second_ids)
