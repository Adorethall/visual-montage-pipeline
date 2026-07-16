"""Stubs: vibe-voice-asr-worker 提供的语音识别能力 (VibeVoice ASR)

远端实现:
  - vibe-voice-asr-transcribe (on_events: audio:transcribe:vibe-voice-asr)

使用方式:
    from stubs.vibe_voice_asr_stubs import vibe_voice_asr_transcribe_stub, VibeVoiceASRInput

    result = await vibe_voice_asr_transcribe_stub.aio_run(
        input=VibeVoiceASRInput(audio_path="s3://media/audio/speech.mp3")
    )

    # 带提示词
    result = await vibe_voice_asr_transcribe_stub.aio_run(
        input=VibeVoiceASRInput(
            audio_path="https://example.com/audio.wav",
            prompt="technical terms: transformer, diffusion",
        )
    )
"""

from hatchet_sdk import Hatchet
from pydantic import AliasChoices, BaseModel, Field,ConfigDict


class VibeVoiceASRInput(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "audio_path": "s3://media/audio/speech.mp3",
                    "prompt": "technical terms: transformer, diffusion",
                },
            ]
        },
    )

    audio_path: str = Field(
        description="Audio file path or URL (local, S3, or HTTP).",
        examples=[
            "s3://media/audio/speech.mp3",
            "https://example.com/audio.wav",
            "./tmp/sample.wav",
        ],
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Optional destination path or object-store prefix for the "
            "transcription JSON.  Defaults to settings.output_prefix."
        ),
        validation_alias=AliasChoices("output_path", "output"),
    )
    prompt: str | None = Field(
        default=None,
        description="Optional hotwords / prompting text to guide transcription.",
        examples=["meeting notes", "technical terms: transformer, diffusion"],
    )


class VibeVoiceASROutput(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "object_path": "s3://media/transcriptions/vibe-voice-asr/vibe-voice-asr-abc123.json",
                    "public_url": "https://s3.adtensor.com/media/transcriptions/vibe-voice-asr/example.json",
                    "language": "en",
                    "model_id": "microsoft/VibeVoice-ASR-HF",
                    "audio_duration_seconds": 42.5,
                    "transcribed_at": "2026-06-04T06:38:21Z",
                }
            ]
        }
    )

    object_path: str | None = Field(
        default=None,
        description=(
            "Storage path of the full transcription JSON. "
            "Contains text, segments, language, timing info."
        ),
        validation_alias=AliasChoices("object_path", "output"),
    )
    public_url: str | None = Field(
        default=None,
        description="Presigned public URL to download the transcription JSON.",
    )
    language: str = Field(default="en", description="Detected language code.")
    model_id: str = Field(description="Model ID used for inference.")
    audio_duration_seconds: float = Field(
        default=0.0,
        description="Duration of the audio in seconds.",
    )
    transcribed_at: str  # ISO 8601 timestamp of when transcription completed.


hatchet = Hatchet()

vibe_voice_asr_transcribe_stub = hatchet.stubs.task(
    name="vibe-voice-asr-transcribe",
    input_validator=VibeVoiceASRInput,
    output_validator=VibeVoiceASROutput,
)


if __name__ == "__main__":
    import asyncio

    async def main():
        result = await vibe_voice_asr_transcribe_stub.aio_run(
            input=VibeVoiceASRInput(
                audio_path="s3://media/audio/demo2-song.mp3",
                prompt="",
            )
        )
        print(result.model_dump_json(indent=2))

    asyncio.run(main())

"""
{
  "object_path": "s3://media/transcriptions/vibe-voice-asr/vibe-voice-asr-5a765a725df7.json",
  "public_url": "https://s3.adtensor.com/media/transcriptions/vibe-voice-asr/vibe-voice-asr-5a765a725df7.json?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=04dKT9QppTmPsKnRGM3V%2F20260604%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20260604T065447Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host&X-Amz-Signature=c03400c9de2d511b984dedbbe4347ba9e3ac96d94856ec9a15866a3a132d4947",
  "language": "en",
  "model_id": "microsoft/VibeVoice-ASR-HF",
  "audio_duration_seconds": 358.75,
  "transcribed_at": "2026-06-04T06:54:47.495645Z"
}
"""