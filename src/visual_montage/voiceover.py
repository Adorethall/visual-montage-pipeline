from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

from .io import load_yaml, write_json
from .storage import get_storage
from .subtitles import generate_subtitles


@contextmanager
def _without_proxy():
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


def voiceover_cache_key(
    *,
    text: str,
    voice: str,
    speed: float,
    provider: str,
    fallback_provider: str,
) -> str:
    payload = json.dumps(
        {
            "text": text,
            "voice": voice,
            "speed": speed,
            "provider": provider,
            "fallback_provider": fallback_provider,
            "version": "1",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _download_and_convert(result, output: Path) -> float:
    suffix = Path(str(result.object_path)).suffix or ".audio"
    with tempfile.TemporaryDirectory(prefix="voiceover-download-") as temp:
        downloaded = Path(temp) / f"source{suffix}"
        get_storage().download_result(
            public_url=result.public_url,
            object_path=result.object_path,
            destination=downloaded,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(downloaded), "-ac", "1", "-ar", "24000",
                "-c:a", "pcm_s16le", str(output),
            ],
            check=True,
        )
    return _probe_duration(output)


def _generate_remote(
    *,
    text: str,
    voice: str,
    speed: float,
    provider: str,
):
    if provider == "omnivoice":
        from worker_stubs.omnivoice import (
            OmniVoiceVoiceDesignInput,
            omnivoice_voice_design_stub,
        )

        allowed = {
            "american accent", "australian accent", "british accent",
            "canadian accent", "child", "chinese accent", "elderly",
            "female", "high pitch", "indian accent", "japanese accent",
            "korean accent", "low pitch", "male", "middle-aged",
            "moderate pitch", "portuguese accent", "russian accent",
            "teenager", "very high pitch", "very low pitch", "whisper",
            "young adult",
        }
        normalized_voice = ", ".join(
            item.strip().lower()
            for item in voice.split(",")
            if item.strip().lower() in allowed
        ) or "female, young adult"
        with _without_proxy():
            return omnivoice_voice_design_stub.run(
                input=OmniVoiceVoiceDesignInput(
                    text=text,
                    voice=normalized_voice,
                    speed=speed,
                    trim_output=True,
                )
            )
    if provider == "voxcpm":
        from worker_stubs.voxcpm import (
            VoxCPMVoiceDesignTaskInput,
            voxcpm_voice_design_stub,
        )

        with _without_proxy():
            return voxcpm_voice_design_stub.run(
                input=VoxCPMVoiceDesignTaskInput(
                    text=text,
                    voice=voice,
                    retry_badcase=True,
                )
            )
    raise ValueError(f"unsupported voiceover provider: {provider}")


def generate_voiceover(
    *,
    campaign_path: Path,
    output: Path,
    cache_dir: Path,
    force: bool = False,
) -> dict:
    load_dotenv()
    campaign = load_yaml(campaign_path)
    voice = campaign.get("voiceover") or {}
    text = str(voice.get("text") or "").strip()
    if not text:
        raise ValueError("campaign voiceover.text is empty")
    voice_description = str(
        voice.get("voice")
        or "female, young adult, natural, energetic, clear"
    )
    speed = float(voice.get("speed") or 1.0)
    provider = str(voice.get("provider") or "omnivoice")
    fallback = str(voice.get("fallback_provider") or "voxcpm")
    maximum = float(voice.get("maximum_duration_seconds") or 8.0)
    cache_key = voiceover_cache_key(
        text=text,
        voice=voice_description,
        speed=speed,
        provider=provider,
        fallback_provider=fallback,
    )
    cache_audio = cache_dir / f"{cache_key}.wav"
    cache_json = cache_dir / f"{cache_key}.json"
    result_path = output.with_name("voiceover-result.json")
    if cache_audio.exists() and cache_json.exists() and not force:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache_audio, output)
        payload = json.loads(cache_json.read_text(encoding="utf-8"))
        payload.update({
            "audio_path": str(output.resolve()),
            "cache_hit": True,
            "forced": False,
        })
        write_json(result_path, payload)
        generate_subtitles(
            audio_path=output,
            source_text=text,
            output=output.with_name("subtitles.json"),
            cache_dir=cache_dir / "subtitles",
            force=False,
        )
        return payload

    attempts = []
    last_error = None
    for selected_provider in dict.fromkeys((provider, fallback)):
        try:
            remote = _generate_remote(
                text=text,
                voice=voice_description,
                speed=speed,
                provider=selected_provider,
            )
            duration = _download_and_convert(remote, output)
            if duration < 0.3 or duration > maximum + 1.0:
                raise ValueError(
                    f"invalid voiceover duration: {duration:.3f}s"
                )
            payload = {
                "provider": selected_provider,
                "requested_provider": provider,
                "fallback_provider": fallback,
                "text": text,
                "voice": voice_description,
                "speed": speed,
                "audio_path": str(output.resolve()),
                "audio_seconds": round(duration, 3),
                "model_id": str(remote.model_id),
                "cache_key": cache_key,
                "cache_hit": False,
                "forced": bool(force),
                "attempts": attempts + [{"provider": selected_provider, "ok": True}],
            }
            cache_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, cache_audio)
            write_json(cache_json, payload)
            write_json(result_path, payload)
            generate_subtitles(
                audio_path=output,
                source_text=text,
                output=output.with_name("subtitles.json"),
                cache_dir=cache_dir / "subtitles",
                force=force,
            )
            return payload
        except Exception as exc:
            last_error = exc
            attempts.append({
                "provider": selected_provider,
                "ok": False,
                "error": str(exc),
            })
    raise RuntimeError(
        f"all voiceover providers failed: {last_error}; attempts={attempts}"
    )
