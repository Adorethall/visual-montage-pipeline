from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Material(BaseModel):
    video_id: str
    path: str
    category: str
    note_id: str = ""
    author: str = ""
    enabled: bool = True


class TimeRange(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "TimeRange":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


class CandidateScores(BaseModel):
    aesthetic: float = Field(default=0, ge=0, le=1)
    category_event_value: float = Field(default=0, ge=0, le=1)
    payoff: float = Field(default=0, ge=0, le=1)
    action_intensity: float = Field(default=0, ge=0, le=1)
    subject_visibility: float = Field(default=0, ge=0, le=1)
    sharpness: float = Field(default=0.5, ge=0, le=1)
    composition: float = Field(default=0.5, ge=0, le=1)
    context_independence: float = Field(default=0.5, ge=0, le=1)


class VisualCandidate(BaseModel):
    candidate_id: str
    video_id: str
    video_path: str
    event: str
    source_window: TimeRange
    preferred_trim: TimeRange
    peak_time: float | None = None
    scores: CandidateScores = Field(default_factory=CandidateScores)
    penalties: dict[str, float] = Field(default_factory=dict)
    roles: list[str] = Field(default_factory=list)
    final_score: float = 0


class TimelineItem(BaseModel):
    timeline_id: str
    role: str
    source_type: Literal["ugc", "product_openpage", "product_recording", "endcard"]
    start: float
    end: float
    candidate_id: str | None = None
    asset_id: str | None = None


class Campaign(BaseModel):
    campaign_id: str
    category: str
    duration_seconds: float = 20
    language: str = "zh-CN"
    aspect_ratio: str = "9:16"
    product_openpage_asset_id: str
    product_recording_asset_id: str
    endcard_asset_id: str
    logo_asset_id: str = ""
    animated_logo_asset_id: str = ""
    cover_logo_asset_id: str = ""
    hook_copy: str = ""
    brand_message: str
    cta: str
    voiceover_text: str


class RunResult(BaseModel):
    ok: bool
    run_id: str
    stage: str
    artifacts: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def ensure_local_materials(materials: list[Material]) -> None:
    missing = [item.path for item in materials if item.enabled and not Path(item.path).expanduser().is_file()]
    if missing:
        raise FileNotFoundError("Missing materials: " + ", ".join(missing))
