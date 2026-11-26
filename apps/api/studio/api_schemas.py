"""Response shapes of the studio API. Names start with ``Studio`` because Ninja merges equal
class names across routers."""

import uuid
from datetime import datetime
from typing import Any

from ninja import Schema

from studio.models import ContentIdea, ContentPiece, StudioRun, TrendItem, TrendSource

EXPOSED_STATE = ("held_reason", "visual_errors", "feedback", "image_trace")


class StudioPieceOut(Schema):
    id: uuid.UUID
    idea_id: uuid.UUID | None
    channel: str
    kind: str
    status: str
    title: str
    brief: str
    pillar: str
    content: dict[str, Any]
    media: list[dict[str, Any]]
    cover_asset_id: uuid.UUID | None
    scheduled_at: datetime | None
    approved_at: datetime | None
    origin: str
    ai_status: str
    ai_error_code: str
    ai_since: datetime | None
    notes: dict[str, Any]
    checks: list[dict[str, Any]]
    manual: bool
    permalink: str
    published_at: datetime | None
    error_code: str
    error_detail: str
    created_at: datetime
    updated_at: datetime


def piece_out(piece: ContentPiece) -> StudioPieceOut:
    return StudioPieceOut(
        id=piece.pk,
        idea_id=piece.idea_id,
        channel=piece.channel,
        kind=piece.kind,
        status=piece.status,
        title=piece.title,
        brief=piece.brief,
        pillar=piece.pillar,
        content=piece.copy,
        media=piece.media,
        cover_asset_id=piece.cover_asset_id,
        scheduled_at=piece.scheduled_at,
        approved_at=piece.approved_at,
        origin=piece.origin,
        ai_status=piece.ai_status,
        ai_error_code=piece.ai_error_code,
        ai_since=piece.ai_since,
        notes={k: v for k, v in piece.ai_state.items() if k in EXPOSED_STATE},
        checks=piece.checks,
        manual=bool(piece.external.get("manual")),
        permalink=piece.permalink,
        published_at=piece.published_at,
        error_code=piece.error_code,
        error_detail=piece.error_detail,
        created_at=piece.created_at,
        updated_at=piece.updated_at,
    )


class StudioIdeaOut(Schema):
    id: uuid.UUID
    title: str
    angle: str
    why_now: str
    pillar: str
    urgency: str
    expires_at: datetime | None
    status: str
    score: float
    format_hints: list[dict[str, Any]]
    needs_from_owner: str
    origin: str
    signals: list[dict[str, str]]
    pieces: int
    created_at: datetime


def idea_out(idea: ContentIdea, signals: list[dict[str, str]], pieces: int) -> StudioIdeaOut:
    return StudioIdeaOut(
        id=idea.pk,
        title=idea.title,
        angle=idea.angle,
        why_now=idea.why_now,
        pillar=idea.pillar,
        urgency=idea.urgency,
        expires_at=idea.expires_at,
        status=idea.status,
        score=idea.score,
        format_hints=idea.format_hints,
        needs_from_owner=idea.needs_from_owner,
        origin=idea.origin,
        signals=signals,
        pieces=pieces,
        created_at=idea.created_at,
    )


class StudioSourceOut(Schema):
    id: uuid.UUID
    kind: str
    name: str
    params: dict[str, Any]
    enabled: bool
    last_fetched_at: datetime | None
    last_count: int
    last_error: str


def source_out(source: TrendSource) -> StudioSourceOut:
    return StudioSourceOut(
        id=source.pk,
        kind=source.kind,
        name=source.name,
        params=source.params,
        enabled=source.enabled,
        last_fetched_at=source.last_fetched_at,
        last_count=source.last_count,
        last_error=source.last_error,
    )


class StudioTrendOut(Schema):
    id: uuid.UUID
    kind: str
    title: str
    summary: str
    url: str
    score: float
    published_at: datetime | None
    ignored: bool
    created_at: datetime


def trend_out(item: TrendItem) -> StudioTrendOut:
    return StudioTrendOut(
        id=item.pk,
        kind=item.kind,
        title=item.title,
        summary=item.summary,
        url=item.url,
        score=item.score,
        published_at=item.published_at,
        ignored=item.ignored,
        created_at=item.created_at,
    )


class StudioRunOut(Schema):
    id: uuid.UUID
    kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    stats: dict[str, Any]
    error_code: str


def run_out(run: StudioRun) -> StudioRunOut:
    return StudioRunOut(
        id=run.pk,
        kind=run.kind,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stats={k: v for k, v in run.stats.items() if k != "inputs"},
        error_code=run.error_code,
    )
