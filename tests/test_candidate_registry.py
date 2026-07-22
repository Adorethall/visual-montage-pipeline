import json
import hashlib
import wave
from pathlib import Path

from visual_montage.batch import _select_for_slots, batch_compose
from visual_montage.audio_bgm import classify_audio_type
from visual_montage.candidate_registry import CandidateRegistry, stable_candidate_id
from visual_montage.models import CandidateScores, TimeRange, TimelineItem, VisualCandidate
from visual_montage.io import load_campaign
from visual_montage.packaging import fixed_package_items, validate_package
from visual_montage.voiceover import (
    generate_voiceover,
    voiceover_cache_key,
)
from visual_montage.subtitles import (
    align_subtitles,
    parse_moss_response,
    wrap_subtitle_text,
)
from visual_montage.marlin_routing import marlin_segment_windows, offset_marlin_result
from visual_montage.montage import Slot, build_slots, loop_beats, validate_timeline


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


def test_subtitle_text_wraps_without_splitting_english_words() -> None:
    wrapped = wrap_subtitle_text(
        "Find makeup looks you love and save your favorites", maximum_units=28
    )
    assert wrapped == "Find makeup looks you love\nand save your favorites"
    assert all(len(line) <= 28 for line in wrapped.splitlines())


def test_subtitle_text_wraps_cjk_by_visual_width() -> None:
    wrapped = wrap_subtitle_text("找到喜欢的妆容随手收藏下来", maximum_units=14)
    assert wrapped == "找到喜欢的妆容\n随手收藏下来"


def test_package_accepts_two_english_voiceover_sentences() -> None:
    campaign = load_campaign(
        Path("data/inputs/campaigns/beauty_20s.yaml")
    )
    assert validate_package(campaign, fixed_package_items(campaign)) == []


def test_marlin_long_video_segmentation_and_timestamp_offset() -> None:
    profile = {
        "marlin_recall": {
            "maximum_video_duration_seconds": 120,
            "segment_duration_seconds": 115,
            "segment_overlap_seconds": 5,
        }
    }
    assert marlin_segment_windows(29, profile) == [(0.0, 29)]
    assert marlin_segment_windows(120, profile) == [(0.0, 120)]
    assert marlin_segment_windows(188.6, profile) == [
        (0.0, 115.0),
        (110.0, 188.6),
    ]
    result = offset_marlin_result(
        {
            "events": [{"start": 3.0, "end": 5.5}],
            "span": {"start": 3.0, "end": 5.5},
        },
        110.0,
    )
    assert result["events"][0] == {"start": 113.0, "end": 115.5}
    assert result["span"] == {"start": 113.0, "end": 115.5}


def test_montage_slots_cover_fixed_package_boundaries() -> None:
    first = build_slots(0.0, 7.2, [1.173, 2.759, 3.817, 5.334, 6.691])
    second = build_slots(12.8, 17.5, [14.095, 15.267])
    assert first[0].start == 0.0
    assert first[-1].end == 7.2
    assert second[0].start == 12.8
    assert second[-1].end == 17.5
    assert all(left.end == right.start for left, right in zip(first, first[1:]))
    assert all(left.end == right.start for left, right in zip(second, second[1:]))


def test_short_bgm_beats_loop_through_second_montage() -> None:
    source_beats = [
        0.069, 1.058, 2.0, 2.759, 3.541, 4.53,
        5.242, 6.185, 7.128, 7.841, 8.783,
    ]
    beats = loop_beats(source_beats, source_duration=9.684, timeline_end=17.5)

    assert 9.753 in beats
    assert 10.742 in beats
    assert 11.684 in beats
    assert 17.5 not in beats

    second = build_slots(12.8, 17.5, beats, target=1.3)
    assert len(second) > 1
    assert second[0].start == 12.8
    assert second[-1].end == 17.5
    assert max(slot.duration for slot in second) < 4.7
    assert all(left.end == right.start for left, right in zip(second, second[1:]))


def test_extreme_batch_mode_reuses_candidates_only_after_first_use() -> None:
    candidates = [candidate(1, "video_1"), candidate(2, "video_2")]
    slots = [Slot(0.0, 1.0), Slot(1.0, 2.0)]
    candidate_counts = {}
    source_counts = {}
    opening_sources = set()
    first = _select_for_slots(
        slots, candidates, {}, candidate_counts, source_counts,
        1, 2, 2, 0.0, 0.0, opening_sources,
    )
    opening_sources.add(first[0].video_id)
    second = _select_for_slots(
        slots, candidates, {}, candidate_counts, source_counts,
        1, 2, 2, 0.0, 0.0, opening_sources,
    )
    assert len(first) == len(second) == 2
    assert {item.candidate_id for item in first} == {
        item.candidate_id for item in second
    }
    assert candidate_counts == {"raw_1": 2, "raw_2": 2}


def test_source_videos_per_creative_uses_exact_distinct_source_count() -> None:
    candidates = [
        candidate(index * 10 + item, f"video_{index}")
        for index in range(1, 6)
        for item in range(1, 5)
    ]
    slots = [Slot(float(index), float(index + 1)) for index in range(8)]
    chosen = _select_for_slots(
        slots,
        candidates,
        {},
        {},
        {},
        4,
        3,
        20,
        0.0,
        0.0,
        set(),
        3,
    )
    assert len(chosen) == len(slots)
    assert len({item.video_id for item in chosen}) == 3
    counts = {
        video_id: sum(item.video_id == video_id for item in chosen)
        for video_id in {item.video_id for item in chosen}
    }
    assert max(counts.values()) <= 4
    assert min(counts.values()) >= 2


def test_bgm_selection_rejects_assets_shorter_than_minimum(tmp_path: Path) -> None:
    registry_path = tmp_path / "bgm.sqlite"

    def bgm_payload(bgm_id: str, duration: float, score: float) -> dict:
        return {
            "bgm_id": bgm_id,
            "category": "travel",
            "source_video_id": f"video_{bgm_id}",
            "source_video_path": f"/{bgm_id}.mp4",
            "source_fingerprint": f"source_{bgm_id}",
            "configuration_fingerprint": "config",
            "audio_fingerprint": f"audio_{bgm_id}",
            "audio_type": "instrumental",
            "selected_audio_path": f"/{bgm_id}.m4a",
            "use_source": "original_mix",
            "best_window": {"start": 0, "end": duration},
            "duration_seconds": duration,
            "bpm": 120,
            "music_score": score,
            "speech_risk": 0,
            "singing_probability": 0,
            "separation_quality": 1,
            "eligible_as_bgm": True,
            "status": "eligible",
        }

    with CandidateRegistry(registry_path) as registry:
        registry.upsert_bgm(bgm_payload("short", 4.9, 0.99))
        registry.upsert_bgm(bgm_payload("long", 5.0, 0.8))
        selected = registry.select_bgms(
            "travel",
            1,
            minimum_score=0.68,
            maximum_speech_risk=0.18,
            same_bgm_max_per_batch=2,
            target_bpm=120,
            minimum_duration_seconds=5.0,
        )
    assert [item["bgm_id"] for item in selected] == ["long"]


def test_timeline_validation_rejects_main_track_gaps() -> None:
    items = [
        TimelineItem(
            timeline_id="highlight",
            role="visual_montage",
            source_type="ugc",
            start=0.0,
            end=6.691,
            candidate_id="candidate",
        ),
        TimelineItem(
            timeline_id="openpage",
            role="product_openpage",
            source_type="product_openpage",
            start=7.2,
            end=8.7,
            asset_id="openpage",
        ),
    ]
    errors = validate_timeline(items, 8.7)
    assert any(
        "gap: highlight/openpage 6.691-7.200" in item
        for item in errors
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


def test_analysis_cache_round_trip(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.sqlite"
    payload = {
        "analysis_route": "gemma_only",
        "candidates": [candidate(1, "video_a").model_dump()],
    }
    with CandidateRegistry(registry_path) as registry:
        assert registry.get_analysis_cache("cache-1") is None
        registry.put_analysis_cache(
            cache_key="cache-1",
            video_id="video_a",
            video_path="/video_a.mp4",
            category="beauty",
            source_fingerprint="source",
            configuration_fingerprint="configuration",
            model_id="gemma",
            payload=payload,
        )
        cached = registry.get_analysis_cache("cache-1")
    assert cached is not None
    assert cached["analysis_route"] == "gemma_only"
    assert len(cached["candidates"]) == 1


def test_audio_type_keeps_lyrics_and_rejects_spoken_overlay() -> None:
    song = classify_audio_type(
        [
            {"label": "Music", "score": 0.9},
            {"label": "Singing", "score": 0.75},
            {"label": "Speech", "score": 0.5},
        ],
        transcript_text="重复的歌词也可能被识别出来",
        duration=20,
    )
    assert song["audio_type"] == "song_with_lyrics"
    assert song["speech_risk"] < 0.18

    spoken = classify_audio_type(
        [
            {"label": "Music", "score": 0.8},
            {"label": "Speech", "score": 0.85},
        ],
        transcript_text="这是持续的产品讲解文本信息密度非常高" * 3,
        duration=20,
    )
    assert spoken["audio_type"] == "music_with_spoken_overlay"
    assert spoken["speech_risk"] > 0.18


def test_voiceover_cached_mode(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign.yaml"
    campaign.write_text(
        """
voiceover:
  text: 第一句。第二句。
  voice: female, clear
  speed: 1.0
  provider: omnivoice
  fallback_provider: voxcpm
"""
    )
    key = voiceover_cache_key(
        text="第一句。第二句。",
        voice="female, clear",
        speed=1.0,
        provider="omnivoice",
        fallback_provider="voxcpm",
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    with wave.open(str(cache / f"{key}.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 48000)
    subtitle_cache = cache / "subtitles"
    subtitle_cache.mkdir()
    subtitle_key = hashlib.sha256(
        (cache / f"{key}.wav").read_bytes()
        + "第一句。第二句。".encode("utf-8")
    ).hexdigest()[:20]
    (subtitle_cache / f"{subtitle_key}.json").write_text(json.dumps({
        "segments": [
            {"text": "第一句", "start": 0.0, "end": 1.0},
            {"text": "第二句", "start": 1.0, "end": 2.0},
        ]
    }))
    (cache / f"{key}.json").write_text(json.dumps({
        "provider": "voxcpm",
        "audio_seconds": 2.0,
        "model_id": "test",
    }))
    output = tmp_path / "run" / "voice.wav"
    result = generate_voiceover(
        campaign_path=campaign,
        output=output,
        cache_dir=cache,
        force=False,
    )
    assert output.exists()
    assert result["cache_hit"] is True


def test_moss_timestamp_parsing_and_subtitle_alignment() -> None:
    parsed = parse_moss_response(
        '{"text":"[0.15][S01]打开Rednote，发现更多美妆灵感。[6.13]"}'
    )
    assert parsed["segments"][0]["start"] == 0.15
    assert parsed["segments"][0]["end"] == 6.13
    segments = align_subtitles(
        source_text=(
            "打开Rednote，发现更多美妆灵感。"
            "搜索喜欢的妆容，把想学的技巧随时收藏下来。"
        ),
        audio_seconds=6.32,
        asr_segments=parsed["segments"],
        silence_boundaries=[1.062, 3.077, 4.739],
    )
    assert [item["text"] for item in segments] == [
        "打开Rednote",
        "发现更多美妆灵感",
        "搜索喜欢的妆容",
        "把想学的技巧随时收藏下来",
    ]
    assert segments[0]["start"] == 0.0
    assert segments[-1]["end"] == 6.32
    assert [item["end"] for item in segments[:-1]] == [
        1.062,
        3.077,
        4.739,
    ]
    assert all(
        left["end"] == right["start"]
        for left, right in zip(segments, segments[1:])
    )


def test_english_subtitles_preserve_word_spaces() -> None:
    segments = align_subtitles(
        source_text=(
            "Open Rednote for endless beauty inspiration. "
            "Find your looks and save your favorite tips."
        ),
        audio_seconds=5.28,
        asr_segments=[],
        silence_boundaries=[2.838],
    )
    assert [item["text"] for item in segments] == [
        "Open Rednote for endless beauty inspiration.",
        "Find your looks and save your favorite tips.",
    ]
    assert all(" " in item["text"] for item in segments)
    assert segments[-1]["end"] == 5.28


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
