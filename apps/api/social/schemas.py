"""Boundary types of the social domain: response settings, action payloads and AI outputs."""

import uuid
from typing import Literal

from ninja import Schema
from pydantic import BaseModel, Field, field_validator

# --- response settings ------------------------------------------------------------------------

ReplyMode = Literal["off", "draft", "auto"]
COMMENT_CATEGORIES = (
    "question",
    "praise",
    "complaint",
    "lead",
    "competitor",
    "off_topic",
    "spam",
    "abuse",
    "sensitive",
    "other",
)
DM_INTENTS = (
    "pricing",
    "availability",
    "support",
    "quote_request",
    "partnership",
    "complaint",
    "spam",
    "other",
)
DEFAULT_RULES: dict[str, ReplyMode] = {
    "question": "draft",
    "praise": "draft",
    "complaint": "draft",
    "lead": "draft",
    "competitor": "off",
    "off_topic": "off",
    "other": "draft",
}
MAX_LIST_ITEMS = 50
MAX_TEXT = 1000
MAX_KNOWLEDGE = 8000


def _short_list(values: list[str], *, limit: int = 60) -> list[str]:
    clean: list[str] = []
    for value in values:
        text = value.strip()
        if text and text.lower() not in {c.lower() for c in clean}:
            clean.append(text[:limit])
    return clean[:MAX_LIST_ITEMS]


class BusinessHours(Schema):
    timezone: str = "UTC"
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    start: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="17:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @field_validator("days")
    @classmethod
    def valid_days(cls, value: list[int]) -> list[int]:
        return sorted({d for d in value if 0 <= d <= 6})  # noqa: PLR2004 - weekday numbers


class CommentSettings(Schema):
    """How comments are handled; ``rules`` maps a category to off, draft (person sends) or auto."""

    enabled: bool = True
    rules: dict[str, ReplyMode] = Field(default_factory=lambda: dict(DEFAULT_RULES))
    hide_spam: bool = False
    hide_abuse: bool = False
    min_confidence: float = Field(default=0.8, ge=0.5, le=1.0)
    delay_minutes: int = Field(default=3, ge=0, le=1440)
    max_auto_per_hour: int = Field(default=20, ge=1, le=200)
    ignore_older_than_days: int = Field(default=14, ge=1, le=90)
    max_reply_chars: int = Field(default=300, ge=40, le=2200)
    tone: str = Field(default="", max_length=MAX_TEXT)
    sign_off: str = Field(default="", max_length=80)
    emoji: Literal["none", "light", "brand"] = "light"
    blocked_words: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)

    @field_validator("rules")
    @classmethod
    def known_categories(cls, value: dict[str, ReplyMode]) -> dict[str, ReplyMode]:
        # Safety categories are never answered automatically.
        clean = {k: v for k, v in value.items() if k in COMMENT_CATEGORIES}
        for category in ("spam", "abuse", "sensitive"):
            clean.pop(category, None)
        return clean

    @field_validator("blocked_words", "allowed_domains")
    @classmethod
    def clean_lists(cls, value: list[str]) -> list[str]:
        return _short_list(value)


class MessageSettings(Schema):
    """How direct messages are handled."""

    enabled: bool = False
    mode: ReplyMode = "draft"
    greeting: str = Field(default="", max_length=500)
    away_message: str = Field(default="", max_length=500)
    business_hours: BusinessHours | None = None
    knowledge_base: str = Field(default="", max_length=MAX_KNOWLEDGE)
    tone: str = Field(default="", max_length=MAX_TEXT)
    handoff_keywords: list[str] = Field(
        default_factory=lambda: ["human", "agent", "person", "call me"]
    )
    opt_out_keywords: list[str] = Field(default_factory=lambda: ["stop", "unsubscribe", "opt out"])
    always_human_intents: list[str] = Field(
        default_factory=lambda: ["complaint", "partnership", "quote_request"]
    )
    min_confidence: float = Field(default=0.85, ge=0.5, le=1.0)
    max_auto_per_hour: int = Field(default=40, ge=1, le=150)
    max_auto_per_conversation: int = Field(default=6, ge=1, le=20)
    allowed_domains: list[str] = Field(default_factory=list)
    capture_leads: bool = True

    @field_validator("handoff_keywords", "opt_out_keywords", "allowed_domains")
    @classmethod
    def clean_lists(cls, value: list[str]) -> list[str]:
        return _short_list(value)

    @field_validator("always_human_intents")
    @classmethod
    def known_intents(cls, value: list[str]) -> list[str]:
        return [v for v in value if v in DM_INTENTS]


class ReplySettings(Schema):
    comments: CommentSettings = Field(default_factory=CommentSettings)
    messages: MessageSettings = Field(default_factory=MessageSettings)


# --- action payloads --------------------------------------------------------------------------


class Connected(BaseModel):
    connection_id: uuid.UUID
    summary: str = ""


class ReplyComment(Connected):
    comment_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2200)


class HideComment(Connected):
    comment_id: str = Field(min_length=1, max_length=100)
    hidden: bool = True


class DeleteComment(Connected):
    comment_id: str = Field(min_length=1, max_length=100)


class SendMessage(Connected):
    conversation_id: uuid.UUID
    recipient_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=1000)


class SetCommentsEnabled(Connected):
    media_id: str = Field(min_length=1, max_length=100)
    enabled: bool


class PublishAsset(BaseModel):
    asset_id: uuid.UUID
    alt_text: str = Field(default="", max_length=1000)


class PublishPost(Connected):
    draft_id: uuid.UUID
    kind: Literal["image", "carousel", "reel", "story"]
    caption: str = Field(default="", max_length=2200)
    media: list[PublishAsset] = Field(min_length=1, max_length=10)
    cover_asset_id: uuid.UUID | None = None
    share_to_feed: bool = True
    location_id: str = Field(default="", max_length=50)
    collaborators: list[str] = Field(default_factory=list, max_length=3)
    first_comment: str = Field(default="", max_length=2200)


# --- AI outputs -------------------------------------------------------------------------------

Sentiment = Literal["positive", "neutral", "negative"]


class CommentTriage(Schema):
    index: int
    category: Literal[
        "question",
        "praise",
        "complaint",
        "lead",
        "competitor",
        "off_topic",
        "spam",
        "abuse",
        "sensitive",
        "other",
    ]
    sentiment: Sentiment
    language: str = Field(max_length=20)
    priority: int = Field(ge=0, le=3)
    confidence: float = Field(ge=0, le=1)
    action: Literal["reply", "hide", "escalate", "ignore"]
    reason: str = Field(max_length=300)
    reply: str = Field(default="", max_length=2200)


class CommentTriageResult(Schema):
    items: list[CommentTriage]


class DmLead(Schema):
    is_lead: bool
    name: str = ""
    need: str = ""
    contact: str = ""
    budget: str = ""
    timeline: str = ""


class DmAnalysis(Schema):
    intent: Literal[
        "pricing",
        "availability",
        "support",
        "quote_request",
        "partnership",
        "complaint",
        "spam",
        "other",
    ]
    urgency: Literal["low", "normal", "high"]
    sentiment: Sentiment
    language: str = Field(max_length=20)
    summary: str = Field(max_length=600)
    reply: str = Field(default="", max_length=1000)
    handoff: bool
    handoff_reason: str = Field(default="", max_length=300)
    confidence: float = Field(ge=0, le=1)
    lead: DmLead


class Priority(Schema):
    title: str
    why: str
    how: str
    impact: Literal["high", "medium", "low"]
    effort: Literal["high", "medium", "low"]


class Experiment(Schema):
    hypothesis: str
    test: str
    metric: str


class NextPost(Schema):
    idea: str
    format: Literal["image", "carousel", "reel", "story"]
    why: str


class SinceLast(Schema):
    assessment: Literal["improved", "unchanged", "worse", "first_report"]
    explanation: str


class AccountAnalysis(Schema):
    verdict: Literal["strong", "steady", "needs_attention"]
    score: int = Field(ge=0, le=100)
    summary: str
    what_works: list[str]
    what_does_not: list[str]
    format_mix: list[str]
    timing: list[str]
    audience: list[str]
    community: list[str]
    priorities: list[Priority] = Field(max_length=8)
    experiments: list[Experiment] = Field(max_length=4)
    next_posts: list[NextPost] = Field(max_length=6)
    risks: list[str]
    since_last_report: SinceLast


class MediaAnalysis(Schema):
    verdict: Literal["winner", "average", "underperformer", "too_early"]
    score: int = Field(ge=0, le=100)
    summary: str
    why: list[str]
    learnings: list[str]
    improvements: list[str]
    repurpose: list[NextPost] = Field(max_length=3)


class PlanItem(Schema):
    day: int = Field(ge=0, le=27)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    kind: Literal["image", "carousel", "reel", "story"]
    pillar: str = Field(max_length=30)
    title: str = Field(max_length=120)
    hook: str = Field(max_length=200)
    brief: str = Field(max_length=800)
    rationale: str = Field(max_length=400)


class ContentPlan(Schema):
    summary: str
    cadence: str
    pillars: list[str] = Field(max_length=6)
    items: list[PlanItem] = Field(min_length=1, max_length=28)


class Slide(Schema):
    headline: str = Field(max_length=90)
    body: str = Field(default="", max_length=240)
    alt_text: str = Field(default="", max_length=300)


class ReelPlan(Schema):
    hook: str = Field(max_length=150)
    beats: list[str] = Field(max_length=10)
    on_screen_text: list[str] = Field(max_length=10)
    shot_list: list[str] = Field(max_length=10)
    duration_seconds: int = Field(ge=5, le=90)
    audio_note: str = Field(default="", max_length=300)
    cta: str = Field(default="", max_length=150)


class PostCopy(Schema):
    title: str = Field(max_length=120)
    caption: str = Field(max_length=2200)
    alternates: list[str] = Field(max_length=2)
    hashtags: list[str] = Field(max_length=15)
    first_comment: str = Field(default="", max_length=600)
    slides: list[Slide] = Field(default_factory=list, max_length=10)
    reel: ReelPlan | None = None
    image_prompt: str = Field(max_length=1200)
    alt_text: str = Field(max_length=500)
    rationale: str = Field(max_length=600)
