from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_visual_batch.py"
SPEC = importlib.util.spec_from_file_location("analyze_visual_batch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def profile() -> dict:
    return {
        "marlin_recall": {
            "gemma_review_context_seconds": 1.5,
            "gemma_review_max_window_seconds": 12.0,
            "gemma_review_max_windows": 8,
            "gemma_review_max_recall_span_seconds": 30.0,
        }
    }


def test_precise_marlin_events_win_over_broad_recalls() -> None:
    marlin = {"queries": [
        {"ok": True, "query_group": "broad", "events": [{"start": 0, "end": 114}]},
        {"ok": True, "query_group": "precise", "events": [{"start": 54, "end": 56}]},
    ]}
    windows = MODULE.build_marlin_review_windows(marlin, 195.0, profile())
    assert windows == [{
        "start": 52.5,
        "end": 57.5,
        "query_groups": ["precise"],
        "descriptions": [],
    }]


def test_broad_recall_falls_back_to_centered_bounded_window() -> None:
    marlin = {"queries": [
        {"ok": True, "query_group": "broad", "events": [{"start": 0, "end": 114}]},
    ]}
    windows = MODULE.build_marlin_review_windows(marlin, 195.0, profile())
    assert windows[0]["start"] == 51.0
    assert windows[0]["end"] == 63.0


def test_gemma_relative_timestamps_are_offset_to_source() -> None:
    raw = {"candidates": [{"start": 1, "end": 3, "peak_time": 2}]}
    adjusted = MODULE.offset_gemma_analysis(raw, 52.5, 57.5)
    candidate = adjusted["candidates"][0]
    assert candidate["start"] == 53.5
    assert candidate["end"] == 55.5
    assert candidate["peak_time"] == 54.5


def test_gemma_retries_transient_transport_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://example.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    calls = {"count": 0}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": json.dumps({"candidates": []})}}],
                "usage": {},
            }

    def post(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ReadError("SSL EOF")
        return Response()

    monkeypatch.setattr(MODULE.httpx, "post", post)
    analysis, _ = MODULE.call_gemma(
        video, "review", 30, max_attempts=3, retry_delays=()
    )
    assert analysis == {"candidates": []}
    assert calls["count"] == 2


def test_gemma_does_not_retry_request_too_large(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_API_BASE", "https://example.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    calls = {"count": 0}

    def post(*_args, **_kwargs):
        calls["count"] += 1
        request = httpx.Request("POST", "https://example.test/v1/chat/completions")
        response = httpx.Response(413, request=request)
        response.raise_for_status()

    monkeypatch.setattr(MODULE.httpx, "post", post)
    with pytest.raises(RuntimeError, match="after 1 attempt"):
        MODULE.call_gemma(video, "review", 30, max_attempts=3, retry_delays=())
    assert calls["count"] == 1


def test_marlin_window_checkpoint_resumes_without_new_api_call(
    monkeypatch, tmp_path: Path
) -> None:
    proxy = tmp_path / "proxies" / "source.mp4"
    proxy.parent.mkdir(parents=True)
    proxy.write_bytes(b"proxy")
    material = SimpleNamespace(video_id="travel_test")
    test_profile = profile()
    test_profile["gemma_review"] = {
        "api_max_attempts": 3,
        "api_retry_delays_seconds": [],
    }
    marlin = {"queries": [{
        "ok": True,
        "query_group": "view",
        "events": [{"start": 10, "end": 12}],
    }]}
    calls = {"gemma": 0, "cut": 0}

    def cut(_proxy, output, _start, _end):
        calls["cut"] += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"clip")

    def gemma(*_args, **_kwargs):
        calls["gemma"] += 1
        return (
            {"candidates": [{"start": 0.5, "end": 1.5, "peak_time": 1.0}]},
            {"usage": {"total_tokens": 10}},
        )

    monkeypatch.setattr(MODULE, "cut_marlin_segment", cut)
    monkeypatch.setattr(MODULE, "call_gemma", gemma)
    first = MODULE.call_gemma_on_marlin_windows(
        proxy, material, test_profile, marlin, "prompt", 30, 30
    )
    second = MODULE.call_gemma_on_marlin_windows(
        proxy, material, test_profile, marlin, "prompt", 30, 30
    )
    assert calls == {"gemma": 1, "cut": 1}
    assert first[2]["windows"][0]["checkpoint_hit"] is False
    assert second[2]["windows"][0]["checkpoint_hit"] is True


def test_candidate_deduplication_keeps_stronger_overlapping_discovery() -> None:
    candidates = [
        {
            "candidate_id": "weak",
            "confidence": 0.5,
            "source_window": {"start": 1.0, "end": 3.0},
        },
        {
            "candidate_id": "strong",
            "confidence": 0.9,
            "source_window": {"start": 1.2, "end": 2.9},
        },
        {
            "candidate_id": "distinct",
            "confidence": 0.7,
            "source_window": {"start": 5.0, "end": 7.0},
        },
    ]
    output = MODULE.deduplicate_candidates(candidates)
    assert [item["candidate_id"] for item in output] == ["strong", "distinct"]


def test_topup_prompt_excludes_existing_intervals() -> None:
    prompt = MODULE.candidate_topup_prompt(
        "base",
        [{
            "event": "view",
            "source_window": {"start": 10.0, "end": 12.0},
        }],
        minimum=3,
        attempt=1,
    )
    assert "at least 2 additional" in prompt
    assert '"start": 10.0' in prompt
    assert "Chinese subtitles remain a hard rejection" in prompt


def test_marlin_review_windows_use_at_most_five_concurrent_gemma_calls(
    monkeypatch, tmp_path: Path
) -> None:
    proxy = tmp_path / "proxies" / "source.mp4"
    proxy.parent.mkdir(parents=True)
    proxy.write_bytes(b"proxy")
    material = SimpleNamespace(video_id="travel_concurrency")
    test_profile = profile()
    test_profile["marlin_recall"]["gemma_review_context_seconds"] = 0
    test_profile["gemma_review"] = {
        "api_max_attempts": 1,
        "api_max_concurrency": 5,
        "api_retry_delays_seconds": [],
    }
    marlin = {"queries": [{
        "ok": True,
        "query_group": "view",
        "events": [
            {"start": start, "end": start + 1}
            for start in (0, 3, 6, 9, 12, 15)
        ],
    }]}
    state = {"active": 0, "maximum": 0}
    lock = threading.Lock()

    def cut(_proxy, output, _start, _end):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"clip")

    def gemma(*_args, **_kwargs):
        with lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return {"candidates": []}, {"usage": {}}

    monkeypatch.setattr(MODULE, "cut_marlin_segment", cut)
    monkeypatch.setattr(MODULE, "call_gemma", gemma)
    result = MODULE.call_gemma_on_marlin_windows(
        proxy, material, test_profile, marlin, "prompt", 30, 30
    )
    assert result[2]["window_count"] == 6
    assert result[2]["max_concurrency"] == 5
    assert state["maximum"] == 5
