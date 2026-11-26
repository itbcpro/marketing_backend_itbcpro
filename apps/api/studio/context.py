"""Facts handed to the prompts: strategy, channel limits, recent output and measured results."""

import dataclasses
import uuid
from typing import Any

from social import ai
from social.models import SocialMedia
from studio.channels.registry import all_channels, get_channel
from studio.config import StudioConfig
from studio.models import Brand, ContentPiece, PieceStatus

RECENT_LIMIT = 25
PERFORMANCE_LIMIT = 6
DESIGN_MEMORY = 8
CAPTION_CHARS = 240
EXCLUDED = (PieceStatus.CANCELLED,)


def strategy_view(config: StudioConfig) -> str:
    """What the writer needs from the settings (scheduling details stay out of the prompt)."""
    return ai.dumps(
        {
            "goals": config.goals,
            "audience": config.audience,
            "offer_notes": config.offer_notes,
            "pillars": [p.model_dump() for p in config.pillars],
            "voice": config.voice.model_dump(),
            "topics_to_follow": config.trends.topics,
            "topics_to_avoid": config.trends.avoid_topics,
            "visual_style": config.visuals.style,
        }
    )


def channels_view(config: StudioConfig) -> str:
    rows: list[dict[str, Any]] = []
    for key in config.enabled_channels():
        plan = config.plan_for(key)
        allowed = plan.kinds or get_channel(key).kind_names
        rows.append({"channel": key, "post_types": allowed, "notes": plan.instructions})
    if not rows:
        rows = [{"channel": c.key, "post_types": c.kind_names, "notes": ""} for c in all_channels()]
    return ai.dumps(rows)


def limits_view(channel: str, kind: str) -> str:
    spec = get_channel(channel).kind(kind)
    return ai.dumps(dataclasses.asdict(spec)) if spec else "{}"


def recent_view(brand_id: uuid.UUID, channel: str | None = None) -> str:
    rows = ContentPiece.objects.filter(brand_id=brand_id).exclude(status__in=EXCLUDED)
    if channel:
        rows = rows.filter(channel=channel)
    items = [
        {
            "channel": p.channel,
            "kind": p.kind,
            "title": p.title,
            "opening": str(p.copy.get("hook") or p.copy.get("body", ""))[:CAPTION_CHARS],
            "status": p.status,
            "at": p.scheduled_at.isoformat() if p.scheduled_at else "",
        }
        for p in rows.order_by("-created_at")[:RECENT_LIMIT]
    ]
    return ai.dumps(items)


def recent_designs(brand_id: uuid.UUID, channel: str) -> str:
    """The art directions of the latest posts, so a new picture is designed to look different."""
    rows = (
        ContentPiece.objects.filter(brand_id=brand_id, channel=channel)
        .exclude(status__in=EXCLUDED)
        .order_by("-created_at")[:DESIGN_MEMORY]
    )
    found = [str(p.copy.get("visual", {}).get("design_direction", "")).strip() for p in rows]
    return ai.dumps([d for d in found if d])


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def account_ids(config: StudioConfig) -> list[str]:
    """The social accounts the brand's channels were pointed at (empty: any account)."""
    return [p.account_id for p in config.channels.values() if p.account_id]


def llm_scope(brand: Brand) -> dict[str, str]:
    """How a prompt names the company. In a workspace that runs several companies the single
    workspace-wide brand profile would describe the wrong one, so the strategy speaks instead."""
    several = (
        Brand.objects.filter(workspace_id=brand.workspace_id, archived_at__isnull=True).count() > 1
    )
    scope = {"display_name": brand.name}
    if several:
        scope["profile_text"] = (
            "(This workspace runs several companies. The strategy above says what this company is.)"
        )
    return scope


def performance_view(workspace_id: uuid.UUID, accounts: list[str] | None = None) -> str:
    """The company's best published posts by engagement rate (empty until accounts are synced)."""
    media = SocialMedia.objects.filter(workspace_id=workspace_id, removed_at__isnull=True)
    if accounts:
        media = media.filter(account_id__in=[uuid.UUID(a) for a in accounts if _is_uuid(a)])
    rows = [
        m
        for m in media.exclude(kind="story").order_by("-posted_at")[:60]
        if m.metrics.get("engagement_rate") is not None
    ]
    rows.sort(key=lambda m: m.metrics.get("engagement_rate") or 0, reverse=True)
    return ai.dumps(
        [
            {"kind": m.kind, "caption": m.caption[:CAPTION_CHARS], "metrics": m.metrics}
            for m in rows[:PERFORMANCE_LIMIT]
        ]
    )
