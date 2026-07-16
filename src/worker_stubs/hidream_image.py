"""Stubs: hidream-o1-image-worker 提供的 AI 图片生成与编辑能力

远端实现:
  - hidream-o1-image-generate (on_events: image:generate:hidream-o1)
    空 reference_image_urls → text-to-image
    非空 reference_image_urls → image-edit

使用方式:
    from stubs.hidream_image_stubs import hidream_o1_image_stub, HiDreamO1ImageInput

    # 文生图
    result = await hidream_o1_image_stub.aio_run(
        input=HiDreamO1ImageInput(prompt="cinematic portrait", aspect_ratio="9:16")
    )

    # 图编辑
    result = await hidream_o1_image_stub.aio_run(
        input=HiDreamO1ImageInput(
            prompt="Change outfit to blue",
            reference_image_urls=["s3://.../portrait.png"],
        )
    )
"""

from typing import Literal

from hatchet_sdk import Hatchet
from pydantic import AliasChoices, BaseModel, Field

HiDreamAspectRatio = Literal["1:1", "16:9", "9:16", "4:3", "3:4"]
HiDreamModelType = Literal["full", "dev"]


class HiDreamO1ImageInput(BaseModel):
    prompt: str = Field(description="Prompt guiding image generation or editing.")
    negative_prompt: str | None = None
    aspect_ratio: HiDreamAspectRatio = "1:1"
    width: int | None = Field(default=None, ge=128, le=4096, multiple_of=16)
    height: int | None = Field(default=None, ge=128, le=4096, multiple_of=16)
    reference_image_urls: list[str] = Field(
        default_factory=list,
        description="Empty → text-to-image, non-empty → image edit.",
    )
    seed: int | None = Field(default=None, description="Random seed.")
    output_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices("output_path", "output"),
    )
    num_inference_steps: int | None = Field(
        default=None, ge=1, le=100,
        validation_alias=AliasChoices("num_inference_steps", "steps"),
    )
    cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    model_type: HiDreamModelType | None = None
    noise_scale: float | None = Field(default=None, ge=0.0)
    shift: float | None = None
    keep_original_aspect: bool | None = None
    layout_bboxes: list[list[float]] | None = Field(
        default=None,
        description="Relative bounding boxes for layout control: [[x1, x2, y1, y2], ...].",
    )


class HiDreamO1ImageOutput(BaseModel):
    mode: str
    prompt: str
    negative_prompt: str = ""
    output_path: str
    public_url: str | None = None
    seed: int = 0
    model_id: str = ""
    width: int = 0
    height: int = 0
    reference_image_count: int = 0
    generated_at: str = ""


hatchet = Hatchet()

hidream_o1_image_stub = hatchet.stubs.task(
    name="hidream-o1-image-generate",
    input_validator=HiDreamO1ImageInput,
    output_validator=HiDreamO1ImageOutput,
)


if __name__ == "__main__":
    from tools import get_storage

    from pathlib import Path
    result = hidream_o1_image_stub.run(
        input=HiDreamO1ImageInput(
            prompt="a big fish eat apple"
        )
    )
    storage = get_storage()
    project_root = Path(__file__).parent.parent.parent
    storage.download_path(
        result.output_path, project_root/"data"/result.output_path.split('/')[-1]
    )
