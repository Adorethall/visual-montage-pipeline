"""Stubs: voxcpm-worker 提供的语音合成能力 (VoxCPM2)

远端实现:
  - voxcpm-tts           (on_events: audio:tts:voxcpm)
  - voxcpm-voice-clone   (on_events: audio:voice-clone:voxcpm)
  - voxcpm-voice-design  (on_events: audio:voice-design:voxcpm)

使用方式:
    from stubs.voxcpm_stubs import voxcpm_tts_stub, VoxCPMTtsTaskInput

    # TTS
    result = await voxcpm_tts_stub.aio_run(
        input=VoxCPMTtsTaskInput(text="你好世界")
    )

    # Voice Clone
    result = await voxcpm_voice_clone_stub.aio_run(
        input=VoxCPMVoiceCloneTaskInput(
            text="请用参考音频的声线朗读这句话。",
            reference_audio="s3://media/reference/voice.wav",
        )
    )

    # Voice Design
    result = await voxcpm_voice_design_stub.aio_run(
        input=VoxCPMVoiceDesignTaskInput(
            text="欢迎使用 VoxCPM。",
            voice="young female voice, warm and gentle",
        )
    )
"""

from typing import Literal

from hatchet_sdk import Hatchet
from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class VoxCPMBaseInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    text: str = Field(description="Text to synthesize.")
    output_path: str | None = Field(
        default=None,
        description="Optional destination object-store prefix or path.",
        validation_alias=AliasChoices("output_path", "output"),
    )
    cfg_value: float | None = Field(default=None, ge=0.1, le=10.0)
    inference_timesteps: int | None = Field(default=None, ge=1, le=100)
    normalize: bool | None = Field(default=None)
    denoise: bool | None = Field(default=None)
    retry_badcase: bool | None = Field(default=None)
    retry_badcase_max_times: int | None = Field(default=None, ge=1, le=10)
    retry_badcase_ratio_threshold: float | None = Field(default=None, ge=1.0, le=20.0)
    min_len: int | None = Field(default=None, ge=1)
    max_len: int | None = Field(default=None, ge=8, le=8192)


class VoxCPMTtsTaskInput(VoxCPMBaseInput):
    """纯 TTS 文本转语音。"""
    pass


class VoxCPMVoiceCloneTaskInput(VoxCPMBaseInput):
    """基于参考音频克隆声线。"""
    reference_audio: str | None = Field(
        default=None,
        description="Reference audio path for voice cloning (s3:// or http://).",
        validation_alias=AliasChoices("reference_audio", "reference_wav_path"),
    )
    prompt_audio: str | None = Field(
        default=None,
        description="Prompt audio path for continuation-style cloning.",
        validation_alias=AliasChoices("prompt_audio", "prompt_wav_path"),
    )
    prompt_text: str | None = Field(
        default=None,
        description="Transcript for prompt audio.",
        validation_alias=AliasChoices("prompt_text", "reference_text"),
    )


class VoxCPMVoiceDesignTaskInput(VoxCPMBaseInput):
    """用自然语言描述定制声线。"""
    voice: str | None = Field(
        default=None,
        description="Voice style instruction, e.g. 'young female voice, warm and gentle'.",
        validation_alias=AliasChoices("voice", "control"),
    )


class VoxCPMTaskOutput(BaseModel):
    mode: Literal["tts", "voice-clone", "voice-design"]
    text: str
    object_path: str = Field(validation_alias=AliasChoices("object_path", "output", "output_path"))
    public_url: str | None = Field(default=None, validation_alias=AliasChoices("public_url", "url"))
    sample_rate: int
    audio_seconds: float | None = None
    model_id: str
    generated_at: str = ""


hatchet = Hatchet()

voxcpm_tts_stub = hatchet.stubs.task(
    name="voxcpm-tts",
    input_validator=VoxCPMTtsTaskInput,
    output_validator=VoxCPMTaskOutput,
)

voxcpm_voice_clone_stub = hatchet.stubs.task(
    name="voxcpm-voice-clone",
    input_validator=VoxCPMVoiceCloneTaskInput,
    output_validator=VoxCPMTaskOutput,
)

voxcpm_voice_design_stub = hatchet.stubs.task(
    name="voxcpm-voice-design",
    input_validator=VoxCPMVoiceDesignTaskInput,
    output_validator=VoxCPMTaskOutput,
)


if __name__ == "__main__":
    from tools import get_storage
    from pathlib import Path

    result = voxcpm_voice_design_stub.run(
        input=VoxCPMVoiceDesignTaskInput(
            text="这是一段 VoxCPM 测试语音。",
            voice="young female voice, warm and gentle",
        )
    )
    storage = get_storage()
    project_root = Path(__file__).parent.parent.parent
    source = result.object_path
    filename = source.split("/")[-1]
    storage.download_path(source, project_root / "data" / filename)
    print(f"Output: {project_root / 'data' / filename}")
