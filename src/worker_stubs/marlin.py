"""Stubs: marlin-worker 提供的视频理解能力 (caption + find)

远端实现:
  - marlin-caption (on_events: video:caption:marlin)
  - marlin-find    (on_events: video:find:marlin)

使用方式:
    from stubs.marlin_stubs import marlin_caption_stub, MarlinCaptionInput

    result = await marlin_caption_stub.aio_run(input=MarlinCaptionInput(video_url=...))
"""

from hatchet_sdk import Hatchet
from pydantic import AliasChoices, BaseModel, Field


class MarlinCaptionInput(BaseModel):
    video_url: str = Field(
        description="Input video path or URL.",
        validation_alias=AliasChoices("video_url", "video", "input", "input_url", "source_url"),
    )
    output_path: str | None = Field(
        default=None,
        description="Optional destination JSON path or object-store prefix.",
        validation_alias=AliasChoices("output_path", "output"),
    )
    max_new_tokens: int | None = Field(default=None, ge=1, le=4096)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class MarlinFindInput(BaseModel):
    video_url: str = Field(
        description="Input video path or URL.",
        validation_alias=AliasChoices("video_url", "video", "input", "input_url", "source_url"),
    )
    output_path: str | None = Field(
        default=None,
        description="Optional destination JSON path or object-store prefix.",
        validation_alias=AliasChoices("output_path", "output"),
    )
    max_new_tokens: int | None = Field(default=None, ge=1, le=4096)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    event: str = Field(
        description="Event query to locate in the video.",
        validation_alias=AliasChoices("event", "query", "event_query"),
    )


class MarlinTaskOutput(BaseModel):
    ok: bool = True
    task: str
    scene: str = ""
    events: list[dict] = Field(default_factory=list)
    span: dict | None = None
    raw: str = ""
    status: str = ""
    duration: float | None = None
    timings: dict[str, float] = Field(default_factory=dict)
    output_path: str
    public_url: str | None = None
    model_id: str
    generated_at: str = ""


marlin_caption_stub = Hatchet().stubs.task(
    name="marlin-caption",
    input_validator=MarlinCaptionInput,
    output_validator=MarlinTaskOutput,
)

marlin_find_stub = Hatchet().stubs.task(
    name="marlin-find",
    input_validator=MarlinFindInput,
    output_validator=MarlinTaskOutput,
)

if __name__ == "__main__":
    from tools import get_storage
    from pathlib import Path

    result = marlin_caption_stub.run(
        input=MarlinCaptionInput(video_url="https://example.com/video.mp4")
    )
    storage = get_storage()
    project_root = Path(__file__).parent.parent.parent
    filename = result.output_path.split("/")[-1]
    storage.download_path(result.output_path, project_root / "data" / filename)
    print(f"Output: {project_root / 'data' / filename}")
