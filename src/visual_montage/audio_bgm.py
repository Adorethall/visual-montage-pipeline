from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .candidate_registry import CandidateRegistry
from .music_features import analyze_audio
from .storage import get_storage


AUDIO_ANALYSIS_VERSION = "1"
_SEPARATOR_DISABLED_REASON: str | None = None


@contextmanager
def _without_local_proxy():
    names = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    )
    saved = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def _score(top_classes: list[dict], names: tuple[str, ...]) -> float:
    output = 0.0
    for item in top_classes:
        label = str(item.get("label") or "").lower()
        if any(name in label for name in names):
            output = max(output, float(item.get("score") or 0))
    return output


def _run_with_timeout(function, timeout_seconds: float):
    results: queue.Queue = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            results.put((True, function()))
        except BaseException as exc:
            results.put((False, exc))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        ok, value = results.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise TimeoutError(f"remote audio task timed out after {timeout_seconds}s") from exc
    if not ok:
        raise value
    return value


def classify_audio_type(
    top_classes: list[dict],
    *,
    transcript_text: str,
    duration: float,
) -> dict:
    music = _score(top_classes, ("music", "musical instrument"))
    singing = _score(top_classes, ("singing", "choir", "vocal music"))
    rap = _score(top_classes, ("rap", "hip hop"))
    speech = _score(top_classes, ("speech", "narration", "conversation"))
    text_density = len("".join(transcript_text.split())) / max(1.0, duration)
    singing_probability = max(singing, rap)
    if music < 0.12 and speech >= 0.25:
        audio_type = "speech_only"
    elif rap >= 0.15:
        audio_type = "rap_song"
    elif singing_probability >= 0.15:
        audio_type = "song_with_lyrics"
    elif music >= 0.20 and speech >= 0.30 and text_density >= 0.8:
        audio_type = "music_with_spoken_overlay"
    elif music >= 0.20 and speech < 0.30:
        audio_type = "instrumental"
    elif music < 0.12:
        audio_type = "ambient_only"
    else:
        audio_type = "uncertain"
    speech_risk = speech
    if audio_type in {"song_with_lyrics", "rap_song"}:
        # Recognizable lyrics are allowed; singing evidence discounts speech risk.
        speech_risk = max(0.0, speech - singing_probability * 0.8)
    return {
        "audio_type": audio_type,
        "music_probability": round(music, 4),
        "speech_probability": round(speech, 4),
        "singing_probability": round(singing_probability, 4),
        "text_density": round(text_density, 4),
        "speech_risk": round(speech_risk, 4),
    }


def extract_audio(video: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video), "-vn", "-ac", "2", "-ar", "32000",
            "-c:a", "pcm_s16le", str(output),
        ],
        check=True,
    )


def _trim_audio(source: Path, start: float, duration: float, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
            "-vn", "-c:a", "aac", "-b:a", "192k", str(output),
        ],
        check=True,
    )


def _best_window(source: Path, duration: float, target: float = 20.0) -> dict:
    if duration <= target + 0.5:
        analysis = analyze_audio(source, min_duration_seconds=min(5.0, duration))
        return {"start": 0.0, "end": duration, "analysis": analysis}
    starts = list(range(0, max(1, math.floor(duration - target)) + 1, 5))
    final_start = max(0, round(duration - target, 3))
    if not starts or abs(starts[-1] - final_start) > 1:
        starts.append(final_start)
    best = None
    with tempfile.TemporaryDirectory(prefix="bgm-windows-") as temp:
        for index, start in enumerate(starts):
            window = Path(temp) / f"{index:03d}.wav"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{float(start):.3f}", "-i", str(source),
                    "-t", f"{target:.3f}", "-ac", "1", "-ar", "22050",
                    "-c:a", "pcm_s16le", str(window),
                ],
                check=True,
            )
            analysis = analyze_audio(window, min_duration_seconds=5)
            score = float(analysis.get("score") or 0)
            if best is None or score > best["score"]:
                best = {
                    "start": float(start),
                    "end": min(duration, float(start) + target),
                    "score": score,
                    "analysis": analysis,
                }
    return best or {"start": 0.0, "end": min(duration, target), "analysis": {}}


def _audio_fingerprint(path: Path, analysis: dict) -> str:
    digest = hashlib.sha256()
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-ac", "1", "-ar", "8000", "-f", "s16le", "-",
        ],
        check=True,
        capture_output=True,
    )
    pcm = result.stdout
    if pcm:
        step = max(1, len(pcm) // (256 * 1024))
        digest.update(pcm[::step][:256 * 1024])
    digest.update(str(round(float(analysis.get("bpm") or 0), 1)).encode())
    digest.update(
        json.dumps((analysis.get("energy_curve") or [])[:32]).encode()
    )
    return digest.hexdigest()


def _remote_audio_evidence(
    mix: Path,
    video_id: str,
    work_dir: Path,
) -> dict:
    global _SEPARATOR_DISABLED_REASON
    from worker_stubs.audio_separator import AudioSeparatorInput, audio_separator_stub
    from worker_stubs.vibe_voice_asr import VibeVoiceASRInput, vibe_voice_asr_transcribe_stub
    from worker_stubs.yamnet import YAMNetClassifyInput, yamnet_classify_stub

    storage = get_storage()
    uploaded = storage.upload_for_worker(
        mix,
        f"visual-montage/audio-inputs/{video_id}.wav",
        86400,
    )
    worker_source = uploaded["object_path"]
    with _without_local_proxy():
        mix_yamnet = _run_with_timeout(
            lambda: yamnet_classify_stub.run(
                input=YAMNetClassifyInput(
                    audio_url=worker_source,
                    preset="detailed",
                    top_k=30,
                    event_top_k=50,
                )
            ),
            90,
        )
        dialogue_yamnet = _run_with_timeout(
            lambda: yamnet_classify_stub.run(
                input=YAMNetClassifyInput(
                    audio_url=worker_source,
                    preset="dialogue",
                    top_k=30,
                    event_top_k=50,
                )
            )
            ,
            90,
        )
    top_classes = list((mix_yamnet.meta or {}).get("top_classes") or [])
    top_classes.extend(
        list((dialogue_yamnet.meta or {}).get("top_classes") or [])
    )
    music_probability = _score(top_classes, ("music", "musical instrument"))
    evidence = {
        "top_classes": top_classes,
        "yamnet": mix_yamnet.model_dump(mode="json"),
        "dialogue_yamnet": dialogue_yamnet.model_dump(mode="json"),
        "transcript_text": "",
        "instrumental_path": None,
        "vocals_path": None,
        "separation_quality": 0.0,
    }
    if music_probability < 0.12:
        return evidence
    if _SEPARATOR_DISABLED_REASON:
        evidence["separation_error"] = _SEPARATOR_DISABLED_REASON
        return evidence

    try:
        with _without_local_proxy():
            separated = _run_with_timeout(
                lambda: audio_separator_stub.run(
                    input=AudioSeparatorInput(
                        audio_path=worker_source,
                        ensemble_preset="vocal_balanced",
                    )
                ),
                90,
            )
    except Exception as exc:
        _SEPARATOR_DISABLED_REASON = str(exc)
        evidence["separation_error"] = _SEPARATOR_DISABLED_REASON
        return evidence
    for stem in separated.files:
        name = stem.stem_name.lower()
        destination = work_dir / (
            "vocals.wav" if "vocal" in name else "instrumental.wav"
        )
        storage.download_result(
            public_url=stem.public_url,
            object_path=stem.output_path,
            destination=destination,
        )
        if "vocal" in name:
            evidence["vocals_path"] = str(destination)
            vocals_source = stem.output_path
            try:
                with _without_local_proxy():
                    vocals_yamnet = _run_with_timeout(
                        lambda: yamnet_classify_stub.run(
                            input=YAMNetClassifyInput(
                                audio_url=vocals_source,
                                preset="detailed",
                                top_k=30,
                            )
                        )
                        ,
                        90,
                    )
                evidence["top_classes"].extend(
                    list((vocals_yamnet.meta or {}).get("top_classes") or [])
                )
            except Exception as exc:
                evidence["vocals_yamnet_error"] = str(exc)
            try:
                with _without_local_proxy():
                    asr = _run_with_timeout(
                        lambda: vibe_voice_asr_transcribe_stub.run(
                            input=VibeVoiceASRInput(audio_path=vocals_source)
                        ),
                        90,
                    )
                transcript_path = work_dir / "transcript.json"
                storage.download_result(
                    public_url=asr.public_url,
                    object_path=asr.object_path,
                    destination=transcript_path,
                )
                transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
                transcript_text = str(
                    transcript.get("text")
                    or transcript.get("transcript")
                    or ""
                )
                if not transcript_text and transcript.get("segments"):
                    transcript_text = " ".join(
                        str(item.get("text") or "")
                        for item in transcript["segments"]
                    )
                evidence["transcript_text"] = transcript_text
                evidence["asr"] = asr.model_dump(mode="json")
            except Exception as exc:
                evidence["asr_error"] = str(exc)
        else:
            evidence["instrumental_path"] = str(destination)
    evidence["separator"] = separated.model_dump(mode="json")
    evidence["separation_quality"] = (
        0.8 if evidence["instrumental_path"] and evidence["vocals_path"] else 0.0
    )
    return evidence


def analyze_video_bgm(
    *,
    video: Path,
    video_id: str,
    category: str,
    source_fingerprint: str,
    profile: dict,
    registry: CandidateRegistry,
    output_dir: Path,
    force: bool = False,
    cache_only: bool = False,
) -> dict:
    config = profile.get("audio_bgm_analysis") or {}
    configuration_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "version": AUDIO_ANALYSIS_VERSION,
                "config": config,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    cached = None if force else registry.get_bgm_for_source(
        video_id, source_fingerprint, configuration_fingerprint
    )
    if cached:
        return {**cached, "cache_hit": True}
    if cache_only:
        return {
            "source_video_id": video_id,
            "eligible_as_bgm": False,
            "status": "audio_cache_miss",
            "cache_hit": False,
        }

    work_dir = output_dir / video_id
    mix = work_dir / "mix.wav"
    try:
        extract_audio(video, mix)
        local = analyze_audio(mix, min_duration_seconds=5)
    except Exception as exc:
        payload = {
            "bgm_id": f"bgm_{hashlib.sha256(video_id.encode()).hexdigest()[:12]}",
            "category": category,
            "source_video_id": video_id,
            "source_video_path": str(video),
            "source_fingerprint": source_fingerprint,
            "configuration_fingerprint": configuration_fingerprint,
            "audio_fingerprint": "",
            "audio_type": "unavailable",
            "selected_audio_path": "",
            "use_source": "none",
            "best_window": {"start": 0, "end": 0},
            "bpm": 0,
            "music_score": 0,
            "speech_risk": 1,
            "singing_probability": 0,
            "separation_quality": 0,
            "eligible_as_bgm": False,
            "status": f"audio_unavailable: {exc}",
        }
        registry.upsert_bgm(payload)
        return payload

    evidence = {}
    remote_error = None
    if config.get("remote_enrichment", True):
        try:
            evidence = _remote_audio_evidence(mix, video_id, work_dir)
        except Exception as exc:
            remote_error = str(exc)
    classification = classify_audio_type(
        list(evidence.get("top_classes") or []),
        transcript_text=str(evidence.get("transcript_text") or ""),
        duration=float(local.get("duration_seconds") or 0),
    )
    if remote_error:
        classification = {
            **classification,
            "audio_type": "uncertain",
            "speech_risk": 1.0,
        }
    audio_type = classification["audio_type"]
    use_source = "original_mix"
    analysis_source = mix
    if audio_type == "music_with_spoken_overlay":
        instrumental = evidence.get("instrumental_path")
        if instrumental:
            analysis_source = Path(instrumental)
            use_source = "accompaniment"
    best = _best_window(
        analysis_source,
        float(local.get("duration_seconds") or 0),
        float(config.get("target_window_seconds", 20)),
    )
    best_analysis = best.get("analysis") or local
    audio_fingerprint = _audio_fingerprint(analysis_source, best_analysis)
    bgm_id = f"bgm_{audio_fingerprint[:16]}"
    selected = output_dir / "selected" / f"{bgm_id}.m4a"
    allowed = set(config.get("allowed_audio_types") or [
        "instrumental",
        "song_with_lyrics",
        "rap_song",
        "music_with_spoken_overlay",
    ])
    separation_quality = float(evidence.get("separation_quality") or 0)
    if use_source == "original_mix":
        separation_quality = 1.0
    music_score = float(best_analysis.get("score") or 0)
    selected_duration = float(best["end"]) - float(best["start"])
    minimum_bgm_duration = float(
        config.get("minimum_bgm_duration_seconds", 5.0)
    )
    eligible = (
        audio_type in allowed
        and selected_duration >= minimum_bgm_duration
        and music_score >= float(config.get("minimum_music_score", 0.68))
        and classification["speech_risk"]
        <= float(config.get("maximum_speech_risk", 0.18))
        and (
            use_source != "accompaniment"
            or separation_quality
            >= float(config.get("minimum_separation_quality", 0.75))
        )
    )
    if remote_error and audio_type == "uncertain":
        eligible = False
    if eligible:
        _trim_audio(
            analysis_source,
            float(best["start"]),
            float(best["end"]) - float(best["start"]),
            selected,
        )
    payload = {
        "bgm_id": bgm_id,
        "category": category,
        "source_video_id": video_id,
        "source_video_path": str(video),
        "source_fingerprint": source_fingerprint,
        "configuration_fingerprint": configuration_fingerprint,
        "audio_fingerprint": audio_fingerprint,
        **classification,
        "selected_audio_path": str(selected) if eligible else "",
        "use_source": use_source,
        "best_window": {
            "start": round(float(best["start"]), 3),
            "end": round(float(best["end"]), 3),
        },
        "bpm": float(best_analysis.get("bpm") or 0),
        "beats": best_analysis.get("beats") or [],
        "duration_seconds": selected_duration,
        "minimum_bgm_duration_seconds": minimum_bgm_duration,
        "music_score": round(music_score, 4),
        "separation_quality": round(separation_quality, 4),
        "eligible_as_bgm": eligible,
        "status": (
            "eligible"
            if eligible
            else (
                "rejected_too_short"
                if selected_duration < minimum_bgm_duration
                else "rejected"
            )
        ),
        "remote_error": remote_error,
        "evidence": evidence,
        "cache_hit": False,
    }
    registry.upsert_bgm(payload)
    return payload
