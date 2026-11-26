"""Settings of the content studio as validated data.

Nothing about a business, a channel mix or a posting rhythm is fixed in code. The owner describes
goals, pillars, voice, visual style and, per channel, how often and how a post goes out; this
module only checks that the description is well formed and fills sensible blanks.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PublishMode = Literal["manual", "review", "auto"]
"""manual: prepared and handed over for posting by hand. review: a person approves, the platform
posts. auto: the platform approves a clean piece itself (needs the automation right)."""
Emoji = Literal["none", "light", "brand"]
VisualProviderKey = Literal["ai_image", "library", "text_card"]
CoverStyle = Literal["designed", "headline", "plain"]
"""designed: the image model draws the finished cover, headline included, and it is proofread.
headline: a plain photo with the headline written on it by the studio. plain: photo only."""
SlidesStyle = Literal["cards", "designed"]

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_LIST = 40
MAX_TEXT = 1500
LAST_WEEKDAY = 6


def _clean_list(values: list[str], *, limit: int = 120) -> list[str]:
    seen: dict[str, str] = {}
    for value in values:
        text = value.strip()[:limit]
        if text:
            seen.setdefault(text.lower(), text)
    return list(seen.values())[:MAX_LIST]


class Pillar(BaseModel):
    """A recurring theme of the content. ``weight`` is its relative share of the calendar."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=300)
    weight: int = Field(default=1, ge=1, le=10)


class ChannelPlan(BaseModel):
    """How one channel is run: whether, what, how often, and who presses publish."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    kinds: list[str] = Field(default_factory=list)
    posts_per_week: int = Field(default=3, ge=0, le=21)
    days: list[int] = Field(default_factory=lambda: [0, 2, 4])
    times: list[str] = Field(default_factory=lambda: ["10:00"])
    publish_mode: PublishMode = "review"
    account_id: str = Field(default="", max_length=64)
    instructions: str = Field(default="", max_length=MAX_TEXT)

    @field_validator("days")
    @classmethod
    def valid_days(cls, value: list[int]) -> list[int]:
        return sorted({d for d in value if 0 <= d <= LAST_WEEKDAY})

    @field_validator("times")
    @classmethod
    def valid_times(cls, value: list[str]) -> list[str]:
        return sorted({t for t in value if HHMM.match(t)})

    @field_validator("kinds")
    @classmethod
    def clean_kinds(cls, value: list[str]) -> list[str]:
        return _clean_list(value, limit=20)


class VoiceConfig(BaseModel):
    """How the brand sounds, and phrases that must never appear."""

    model_config = ConfigDict(extra="ignore")

    tone: str = Field(default="", max_length=MAX_TEXT)
    language: str = Field(default="English", max_length=60)
    do: list[str] = Field(default_factory=list)
    dont: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    emoji: Emoji = "light"
    first_person: Literal["we", "i", "neutral"] = "we"

    @field_validator("do", "dont", "banned_phrases")
    @classmethod
    def clean(cls, value: list[str]) -> list[str]:
        return _clean_list(value, limit=200)


def _default_prefer() -> list[VisualProviderKey]:
    return ["library", "ai_image", "text_card"]


class VisualConfig(BaseModel):
    """What pictures should look like and where they come from, in order of preference."""

    model_config = ConfigDict(extra="ignore")

    style: str = Field(default="", max_length=MAX_TEXT)
    avoid: list[str] = Field(default_factory=list)
    prefer: list[VisualProviderKey] = Field(default_factory=_default_prefer)
    video_provider: str = Field(default="cards", max_length=40)
    brand_colours_on_cards: bool = True
    brand_colours: list[str] = Field(default_factory=list, max_length=4)
    cover_style: CoverStyle = "designed"
    logo_asset_id: str = Field(default="", max_length=64)
    logo_on_pictures: bool = True
    slides_style: SlidesStyle = "cards"

    @field_validator("avoid")
    @classmethod
    def clean_avoid(cls, value: list[str]) -> list[str]:
        return _clean_list(value, limit=200)

    @field_validator("prefer")
    @classmethod
    def unique_order(cls, value: list[VisualProviderKey]) -> list[VisualProviderKey]:
        return list(dict.fromkeys(value))


class TrendConfig(BaseModel):
    """What counts as relevant news for this business."""

    model_config = ConfigDict(extra="ignore")

    geo: str = Field(default="", max_length=8)
    topics: list[str] = Field(default_factory=list)
    avoid_topics: list[str] = Field(default_factory=list)
    max_age_days: int = Field(default=7, ge=1, le=60)

    @field_validator("topics", "avoid_topics")
    @classmethod
    def clean(cls, value: list[str]) -> list[str]:
        return _clean_list(value, limit=100)


class AutomationConfig(BaseModel):
    """The daily routine: research, plan ahead, write, and (per channel mode) approve."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    run_hour: int = Field(default=6, ge=0, le=23)
    horizon_days: int = Field(default=7, ge=1, le=30)
    idea_buffer: int = Field(default=3, ge=0, le=20)


class StudioConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goals: str = Field(default="", max_length=MAX_TEXT)
    audience: str = Field(default="", max_length=MAX_TEXT)
    offer_notes: str = Field(default="", max_length=MAX_TEXT)
    pillars: list[Pillar] = Field(default_factory=list, max_length=8)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    visuals: VisualConfig = Field(default_factory=VisualConfig)
    trends: TrendConfig = Field(default_factory=TrendConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    channels: dict[str, ChannelPlan] = Field(default_factory=dict)

    def plan_for(self, channel: str) -> ChannelPlan:
        return self.channels.get(channel) or ChannelPlan()

    def enabled_channels(self) -> list[str]:
        return [key for key, plan in self.channels.items() if plan.enabled]
