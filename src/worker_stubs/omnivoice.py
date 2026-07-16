"""Stubs: omnivoice-worker 提供的语音合成能力

远端实现:
  - omnivoice-tts          (on_events: audio:tts:omnivoice)
  - omnivoice-voice-clone  (on_events: audio:voice-clone:omnivoice)
  - omnivoice-voice-design (on_events: audio:voice-design:omnivoice)

使用方式:
    from stubs.omnivoice_stubs import omnivoice_voice_design_stub, OmniVoiceVoiceDesignInput

    result = await omnivoice_voice_design_stub.aio_run(
        input=OmniVoiceVoiceDesignInput(text="...", voice="female, young adult, clear")
    )
"""

from hatchet_sdk import Hatchet
from pydantic import AliasChoices, BaseModel, Field


class OmniVoiceBaseInput(BaseModel):
    text: str = Field(description="Text to synthesize.")
    output_path: str | None = Field(
        default=None,
        description="Optional destination path or object-store prefix.",
        validation_alias=AliasChoices("output_path", "output"),
    )
    steps: int | None = Field(default=None, ge=1, le=128, description="Diffusion steps.")
    guidance: float | None = Field(default=None, gt=0, description="Guidance scale.")
    speed: float | None = Field(default=None, gt=0)
    duration: float | None = Field(default=None, gt=0)
    chunk_seconds: float | None = Field(
        default=None, gt=0,
        validation_alias=AliasChoices("chunk_seconds", "audio_chunk_duration"),
    )
    chunk_threshold_seconds: float | None = Field(
        default=None, gt=0,
        validation_alias=AliasChoices("chunk_threshold_seconds", "audio_chunk_threshold"),
    )
    clean_reference: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("clean_reference", "preprocess_prompt"),
    )
    trim_output: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("trim_output", "postprocess_output"),
    )


class OmniVoiceTtsInput(OmniVoiceBaseInput):
    pass


class OmniVoiceVoiceCloneInput(OmniVoiceBaseInput):
    reference_audio: str = Field(
        description="Reference audio path or URL.",
        validation_alias=AliasChoices("reference_audio", "ref_audio"),
    )
    reference_text: str | None = Field(
        default=None,
        description="Optional transcription for the reference clip.",
        validation_alias=AliasChoices("reference_text", "ref_text"),
    )


class OmniVoiceVoiceDesignInput(OmniVoiceBaseInput):
    voice: str = Field(
        description="Speaker attributes, e.g. 'female, young adult, british accent'.",
        validation_alias=AliasChoices("voice", "instruct"),
    )


class OmniVoiceTaskOutput(BaseModel):
    mode: str
    text: str
    object_path: str
    public_url: str | None = None
    sample_rate: int = 24000
    audio_seconds: float | None = None
    model_id: str = ""
    generated_at: str = ""


hatchet = Hatchet()

omnivoice_tts_stub = hatchet.stubs.task(
    name="omnivoice-tts",
    input_validator=OmniVoiceTtsInput,
    output_validator=OmniVoiceTaskOutput,
)

omnivoice_voice_clone_stub = hatchet.stubs.task(
    name="omnivoice-voice-clone",
    input_validator=OmniVoiceVoiceCloneInput,
    output_validator=OmniVoiceTaskOutput,
)

omnivoice_voice_design_stub = hatchet.stubs.task(
    name="omnivoice-voice-design",
    input_validator=OmniVoiceVoiceDesignInput,
    output_validator=OmniVoiceTaskOutput,
)

if __name__ == "__main__":
    from tools import get_storage
    from pathlib import Path

    result = omnivoice_tts_stub.run(
        input=OmniVoiceTtsInput(
            text="这是一段测试语音。",
        )
    )
    storage = get_storage()
    project_root = Path(__file__).parent.parent.parent
    filename = result.object_path.split("/")[-1]
    storage.download_path(result.object_path, project_root / "data" / filename)
    print(f"Output: {project_root / 'data' / filename}")
