"""Shapes the AI must answer with. Prompts live in ``prompts/studio``; these are the contracts."""

from typing import Literal

from pydantic import BaseModel, Field

from social.schemas import ReelPlan, Slide

Urgency = Literal["evergreen", "timely"]


class FormatHint(BaseModel):
    channel: str = Field(max_length=20)
    kind: str = Field(max_length=10)


class IdeaOut(BaseModel):
    title: str = Field(max_length=160)
    angle: str = Field(max_length=700)
    why_now: str = Field(max_length=400)
    pillar: str = Field(max_length=40)
    urgency: Urgency
    shelf_life_days: int = Field(ge=1, le=90)
    trends: list[int] = Field(max_length=5)
    formats: list[FormatHint] = Field(max_length=8)
    needs_from_owner: str = Field(default="", max_length=300)
    score: int = Field(ge=1, le=10)


class IdeaBatch(BaseModel):
    ideas: list[IdeaOut] = Field(max_length=30)
    notes: str = Field(default="", max_length=600)


class VisualBrief(BaseModel):
    """What the picture shows and says: the scene, the exact words on it, and its art direction."""

    scene: str = Field(max_length=600)
    shot: str = Field(max_length=240)
    on_image_text: str = Field(default="", max_length=120)
    support_line: str = Field(default="", max_length=120)
    poster_lines: list[str] = Field(default_factory=list, max_length=9)
    design_direction: str = Field(default="", max_length=800)
    alt_text: str = Field(max_length=400)
    library_keywords: list[str] = Field(max_length=6)


class TextCheck(BaseModel):
    """What a proofreader read in a designed picture."""

    read_text: list[str] = Field(max_length=30)
    ok: bool
    problems: list[str] = Field(default_factory=list, max_length=10)


class PieceCopy(BaseModel):
    title: str = Field(max_length=120)
    body: str = Field(max_length=4000)
    hook: str = Field(max_length=240)
    hashtags: list[str] = Field(max_length=12)
    cta: str = Field(default="", max_length=240)
    first_comment: str = Field(default="", max_length=600)
    alternate_hooks: list[str] = Field(max_length=2)
    slides: list[Slide] = Field(default_factory=list, max_length=10)
    video: ReelPlan | None = None
    visual: VisualBrief
    rationale: str = Field(max_length=500)
