"""Stubs: yamnet-worker 提供的音频事件分类能力

远端实现:
  - yamnet-classify (on_events: audio:classify:yamnet)

YAMNet 预测 521 个 AudioSet 音频事件类别。完整事件列表保存在输出 JSON 文件中；
Hatchet 响应只返回元数据（不含具体事件），以保持 payload 小巧。

使用方式:
    from stubs.yamnet_stubs import yamnet_classify_stub, YAMNetClassifyInput

    result = await yamnet_classify_stub.aio_run(
        input=YAMNetClassifyInput(audio_url="s3://media/audio/speech.wav")
    )

    # 使用场景预设
    result = await yamnet_classify_stub.aio_run(
        input=YAMNetClassifyInput(
            audio_url="https://example.com/music.mp3",
            preset="music",
            event_top_k=5,
        )
    )
"""

from datetime import datetime

from hatchet_sdk import Hatchet
from pydantic import AliasChoices, BaseModel, Field, ConfigDict


class YAMNetClassifyInput(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "audio_url": "s3://media/audio/speech.wav",
                    "preset": "dialogue",
                    "output_path": "s3://media/audio/yamnet/",
                },
                {
                    "audio_url": "https://example.com/sound.wav",
                    "preset": "music",
                    "top_k": 20,
                },
            ]
        },
    )

    # ── Required ────────────────────────────────────────────
    audio_url: str = Field(
        description="Audio file path or URL (local, S3, or HTTP). "
        "YAMNet expects 16 kHz mono WAV; other formats are auto-converted.",
        examples=[
            "s3://media/audio/speech.wav",
            "https://example.com/sound.wav",
            "/data/sample.wav",
        ],
        validation_alias=AliasChoices("audio_url", "audio", "input", "input_url", "source_url"),
    )

    # ── Scene preset ────────────────────────────────────────
    preset: str | None = Field(
        default=None,
        description=(
            "Scene preset for tuned event extraction.\n"
            "  'dialogue'   — prioritise human sounds, emotions, speech\n"
            "  'music'      — optimise for music / instrument detection\n"
            "  'detailed'   — extract as many distinct event types as possible\n"
            "  'general'    — balanced extraction across all 521 classes\n"
            "Omit or set null to use the worker default (dialogue)."
        ),
        validation_alias=AliasChoices("preset", "scene_preset", "scene"),
    )

    # ── Scoring overrides ───────────────────────────────────
    top_k: int | None = Field(
        default=None, ge=1, le=521,
        description="Number of top classes to return (overrides worker default 10).",
    )
    score_threshold: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Minimum score threshold (overrides worker default 0.1).",
    )

    # ── Event extraction overrides ──────────────────────────
    event_top_k: int | None = Field(
        default=None, ge=1, le=521,
        description="Number of top classes to extract events from (overrides worker default). "
        "Higher = more event variety (50+ recommended for video editing).",
    )
    min_event_score: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Minimum frame-level score for event extraction (overrides worker default 0.25).",
    )
    merge_gap_frames: int | None = Field(
        default=None, ge=1, le=20,
        description="Max gap frames to merge same-class events (5 frames ≈ 2.4s).",
    )
    min_event_frames: int | None = Field(
        default=None, ge=1, le=10,
        description="Minimum event duration in frames (2 frames ≈ 0.96s).",
    )

    # ── Sliding window overrides ────────────────────────────
    enable_sliding_window: bool | None = Field(
        default=None,
        description="Enable sliding-window processing for long audio (default true).",
    )
    window_seconds: float | None = Field(
        default=None, ge=10.0, le=300.0,
        description="Sliding window duration in seconds (overrides worker default 60s).",
    )
    window_overlap_seconds: float | None = Field(
        default=None, ge=0.0, le=30.0,
        description="Overlap between adjacent windows (overrides worker default 5s).",
    )

    # ── Output ──────────────────────────────────────────────
    output_path: str | None = Field(
        default=None,
        description="Optional destination path or object-store prefix for the full classification JSON.",
        validation_alias=AliasChoices("output_path", "output"),
    )


class YAMNetEvent(BaseModel):
    """单个音频事件检测结果。"""

    id: int = Field(description="Event ID (sequential within this classification).")
    label: str = Field(description="Event class label (parent category if hierarchical, else AudioSet class name).")
    score: float = Field(description="Peak confidence score [0–1] for this event.")
    confidence: float = Field(description="Mean confidence score across the event duration.")
    start_time: float = Field(description="Start time in seconds.")
    end_time: float = Field(description="End time in seconds.")
    centroid_time: float = Field(description="Score-weighted centroid time in seconds (more precise localization).")
    subclasses: list[str] | None = Field(
        default=None,
        description="List of sub-class labels that compose this parent-category event (hierarchical mode only).",
    )
    parent: str | None = Field(
        default=None,
        description="Parent category label (hierarchical mode only).",
    )
    subclass: str | None = Field(
        default=None,
        description="Original sub-class label (hierarchical mode only, deprecated in favor of subclasses).",
    )


class YAMNetClassifyOutput(BaseModel):
    """Hatchet task response — metadata only, events are in the JSON file."""

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "task": "Classify",
                    "meta": {
                        "audio_url": "s3://media/audio/speech.wav",
                        "duration": 6.73,
                        "num_classes": 10,
                        "num_events": 3,
                        "top_classes": [
                            {"label": "Speech", "score": 0.95},
                            {"label": "Music", "score": 0.10},
                        ],
                    },
                    "output_path": "s3://media/audio/yamnet/result.json",
                    "public_url": "https://minio.example.com/media/audio/yamnet/result.json",
                    "status": "Classification completed in 2.1s | 6.7s audio | 3 event detections",
                    "generated_at": "2026-06-09T10:00:00Z",
                }
            ]
        },
    )

    task: str = Field(description="Task type, always 'Classify'")
    meta: dict = Field(description="Summary metadata (duration, counts, top classes)")
    output_path: str = Field(description="Path to the saved full result JSON file")
    public_url: str | None = Field(
        default=None,
        description="Publicly accessible URL for the full result JSON (e.g. presigned S3 URL)",
    )
    status: str = Field(description="Human-readable status message with timing and results summary")
    generated_at: datetime = Field(description="Timestamp when the classification was generated (UTC)")


hatchet = Hatchet()

yamnet_classify_stub = hatchet.stubs.task(
    name="yamnet-classify",
    input_validator=YAMNetClassifyInput,
    output_validator=YAMNetClassifyOutput,
)


if __name__ == "__main__":
    import asyncio

    async def main() -> None:
        result = await yamnet_classify_stub.aio_run(
            input=YAMNetClassifyInput(
                audio_url="s3://media/audio/speech.wav",
                preset="dialogue",
            )
        )
        print(result.model_dump_json(indent=2))

    asyncio.run(main())


# example output json (stored in the JSON file; the Hatchet response only returns the top-level fields):
"""
{
  "task": "Classify",
  "meta": {
    "audio_url": "s3://media/audio/speech.wav",
    "duration": 1.94,
    "num_classes": 5,
    "num_events": 2,
    "top_classes": [
      {"label": "Speech", "score": 0.995196},
      {"label": "Music", "score": 0.074264},
      {"label": "Other", "score": 0.003426},
      {"label": "Indoor / Room", "score": 0.00158},
      {"label": "Animal", "score": 2e-05}
    ]
  },
  "events": [
    {
      "id": 0,
      "label": "Speech",
      "score": 0.998077,
      "confidence": 0.995324,
      "start_time": 0.0,
      "end_time": 2.88,
      "centroid_time": 0.719,
      "subclasses": ["Speech"]
    },
    {
      "id": 1,
      "label": "Music",
      "score": 0.183022,
      "confidence": 0.135474,
      "start_time": 0.96,
      "end_time": 2.88,
      "centroid_time": 1.284,
      "subclasses": ["Speech synthesizer"]
    }
  ],
  "output_path": "s3://media/audio/yamnet/yamnet-72e4d2719e0c.json",
  "public_url": "https://s3.adtensor.com/media/audio/yamnet/yamnet-72e4d2719e0c.json?X-Amz-Algorithm=...",
  "status": "Classification completed in 1.4s | 1.9s audio | 5 classes | 2 event detections",
  "generated_at": "2026-06-09T09:18:46.729858Z"
}
"""