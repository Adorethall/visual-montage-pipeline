"""Stubs: rmbg-worker 提供的背景移除能力

远端实现:
  - rmbg-remove-background (on_events: image:remove-background:rmbg)

使用方式:
    from stubs.rmbg_stubs import rmbg_remove_background_stub, RMBGInput

    result = await rmbg_remove_background_stub.aio_run(
        input=RMBGInput(image_url="s3://...")
    )
"""

from hatchet_sdk import Hatchet
from pydantic import AliasChoices, BaseModel, Field


class RMBGInput(BaseModel):
    image_url: str = Field(
        description="Single source image path or URL.",
        validation_alias=AliasChoices("image_url", "input", "input_url", "source_url"),
    )
    output_path: str | None = Field(
        default=None,
        description="Optional destination path for the output PNG.",
        validation_alias=AliasChoices("output_path", "output"),
    )


class RMBGOutput(BaseModel):
    output_path: str
    public_url: str | None = None


hatchet = Hatchet()

rmbg_remove_background_stub = hatchet.stubs.task(
    name="rmbg-remove-background",
    input_validator=RMBGInput,
    output_validator=RMBGOutput,
)

if __name__ == "__main__":
    from tools import get_storage
    from pathlib import Path

    result = rmbg_remove_background_stub.run(
        input=RMBGInput(image_url="s3://media/image/hidream-o1-image/hidream-o1-image-b303a52b3cb3.png")
    )
    storage = get_storage()
    project_root = Path(__file__).parent.parent.parent
    filename = result.output_path.split("/")[-1]
    storage.download_path(result.output_path, project_root / "data" / filename)
    print(f"Output: {project_root / 'data' / filename}")
