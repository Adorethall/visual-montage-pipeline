"""Stubs: firered-image-edit-worker 提供的 AI 图片编辑能力

远端实现:
  - firered-image-edit-generate (on_events: image:edit:firered)

使用方式:
    from stubs.firered_image_stubs import firered_image_edit_stub, FireRedImageEditInput

    result = await firered_image_edit_stub.aio_run(
        input=FireRedImageEditInput(
            prompt="...",
            reference_image_urls=["s3://..."],
        )
    )
"""

from typing import Literal

from hatchet_sdk import Hatchet
from pydantic import AliasChoices, BaseModel, Field

FireRedAspectRatio = Literal["1:1", "16:9", "9:16", "4:3", "3:4"]


class FireRedImageEditInput(BaseModel):
    prompt: str = Field(description="Prompt guiding the image edit.")
    aspect_ratio: FireRedAspectRatio = Field(default="9:16")
    reference_image_urls: list[str] = Field(
        default_factory=list,
        description="Optional reference image URLs, up to 3.",
    )
    seed: int | None = Field(default=None, description="Random seed.")
    output_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices("output_path", "output"),
    )
    num_inference_steps: int | None = Field(
        default=None, ge=1, le=80,
        validation_alias=AliasChoices("num_inference_steps", "steps"),
    )
    true_cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    guidance_scale: float | None = Field(
        default=None, ge=0.0, le=20.0,
        validation_alias=AliasChoices("guidance_scale", "guidance"),
    )
    negative_prompt: str | None = None
    edit_image_auto_resize: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("edit_image_auto_resize", "auto_resize"),
    )


class FireRedImageEditOutput(BaseModel):
    prompt: str
    aspect_ratio: FireRedAspectRatio
    output_path: str
    public_url: str | None = None
    seed: int = 0
    model_id: str = ""
    width: int = 0
    height: int = 0
    reference_image_count: int = 0
    generated_at: str = ""


hatchet = Hatchet()

firered_image_edit_stub = hatchet.stubs.task(
    name="firered-image-edit-generate",
    input_validator=FireRedImageEditInput,
    output_validator=FireRedImageEditOutput,
)


if __name__ == "__main__":
    from tools import get_storage

    from pathlib import Path
    result = firered_image_edit_stub.run(
        input=FireRedImageEditInput(
            prompt="a big fish eat apple"
        )
    )
    storage = get_storage()
    project_root = Path(__file__).parent.parent.parent
    storage.download_path(
        result.output_path, project_root/"data"/result.output_path.split('/')[-1]
    )
