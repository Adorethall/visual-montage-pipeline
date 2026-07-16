"""Stubs: audio-separator-worker 提供的音频分离能力

远端实现:
  - audio-separator-separate (on_events: audio:separate)
    将音频/视频文件中的人声和伴奏分离

使用方式:
    from stubs.audio_separator_stubs import audio_separator_stub, AudioSeparatorInput

    # 基础分离
    result = await audio_separator_stub.aio_run(
        input=AudioSeparatorInput(
            audio_path="s3://media/audio/song.mp3",
            ensemble_preset="vocal_balanced",
        )
    )
    print(result.files)  # [StemFile(stem_name="Vocals", ...), StemFile(stem_name="Instrumental", ...)]
"""

from __future__ import annotations

from hatchet_sdk import Hatchet
from pydantic import AliasChoices, BaseModel, Field


class AudioSeparatorInput(BaseModel):
    """Input for audio source separation.

    Supports any audio file (wav, mp3, flac, etc.) and video files
    (mp4, mov, avi, etc.) — video inputs are automatically transcoded
    to WAV before separation.
    """

    audio_path: str = Field(
        description="Audio file path or URL (S3, HTTP, or local on k8s node).",
        examples=[
            "s3://media/audio/song.mp3",
            "https://example.com/audio.wav",
            "/jfs/smoke-data/sample.wav",
        ],
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Optional destination prefix for separated stems. "
            "Defaults to worker's configured output_prefix."
        ),
        validation_alias=AliasChoices("output_path", "output"),
    )
    ensemble_preset: str | None = Field(
        default=None,
        description=(
            "Ensemble preset name. Available: vocal_balanced (default), "
            "vocal_clean, vocal_full, vocal_rvc, instrumental_clean, "
            "instrumental_full, instrumental_balanced, "
            "instrumental_low_resource, karaoke."
        ),
        examples=["vocal_balanced", "karaoke", "instrumental_clean"],
    )


class StemFile(BaseModel):
    """A single separated stem file."""

    stem_name: str = Field(description="Stem name (e.g. Vocals, Instrumental)")
    output_path: str = Field(description="Storage path of the stem file")
    public_url: str | None = Field(default=None, description="Presigned public URL")


class AudioSeparatorOutput(BaseModel):
    """Output of a successful audio separation run."""

    preset: str = Field(description="Ensemble preset used")
    source_file: str = Field(description="Original input filename")
    files: list[StemFile] = Field(description="List of separated stem files")
    infer_seconds: float = Field(default=0.0, description="Time spent on inference")
    run_id: str = Field(default="", description="Unique run identifier")


hatchet = Hatchet()

audio_separator_stub = hatchet.stubs.task(
    name="audio-separator-separate",
    input_validator=AudioSeparatorInput,
    output_validator=AudioSeparatorOutput,
)


if __name__ == "__main__":
    import asyncio

    async def demo() -> None:
        result = await audio_separator_stub.aio_run(
            input=AudioSeparatorInput(
                audio_path="s3://media/audio/speech.wav",
                ensemble_preset="vocal_balanced",
            )
        )
        print(f"Preset: {result.preset}")
        print(f"Source: {result.source_file}")
        print(f"Files ({len(result.files)}):")
        for f in result.files:
            print(f"  • {f.stem_name}: {f.output_path}")
            if f.public_url:
                print(f"    URL: {f.public_url}")
        print(f"Infer: {result.infer_seconds:.2f}s")
        print(f"Run ID: {result.run_id}")

    asyncio.run(demo())
