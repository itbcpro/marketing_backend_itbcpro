"""Content studio: drafts, the AI composer, approval and the calendar."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema

from core.errors import ValidationFailedError
from identity.authentication import api_auth
from social import checks, composer, drafts
from social.api import account_of, user_of
from social.models import DraftStatus, PostDraft
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["instagram-studio"], auth=api_auth)

MAX_RANGE_DAYS = 100
MAX_PAGE = 100


class IgDraftOut(Schema):
    id: uuid.UUID
    kind: str
    status: str
    title: str
    brief: str
    caption: str
    hashtags: list[str]
    first_comment: str
    media: list[dict[str, Any]]
    cover_asset_id: uuid.UUID | None
    share_to_feed: bool
    location_id: str
    collaborators: list[str]
    scheduled_at: datetime | None
    approved_at: datetime | None
    pillar: str
    origin: str
    ai: dict[str, Any]
    ai_status: str
    ai_error_code: str
    ai_since: datetime | None
    checks: list[dict[str, Any]]
    action_id: uuid.UUID | None
    permalink: str
    published_at: datetime | None
    error_code: str
    error_detail: str
    created_at: datetime
    updated_at: datetime


def draft_out(draft: PostDraft) -> IgDraftOut:
    return IgDraftOut(
        id=draft.pk,
        kind=draft.kind,
        status=draft.status,
        title=draft.title,
        brief=draft.brief,
        caption=draft.caption,
        hashtags=[str(h) for h in draft.hashtags],
        first_comment=draft.first_comment,
        media=draft.media,
        cover_asset_id=draft.cover_asset_id,
        share_to_feed=draft.share_to_feed,
        location_id=draft.location_id,
        collaborators=[str(c) for c in draft.collaborators],
        scheduled_at=draft.scheduled_at,
        approved_at=draft.approved_at,
        pillar=draft.pillar,
        origin=draft.origin,
        ai=draft.ai,
        ai_status=draft.ai_status,
        ai_error_code=draft.ai_error_code,
        ai_since=draft.ai_since,
        checks=draft.checks,
        action_id=draft.action_id,
        permalink=draft.permalink,
        published_at=draft.published_at,
        error_code=draft.error_code,
        error_detail=draft.error_detail,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


class IgDraftFilters(Schema):
    status: str = ""
    start: datetime | None = None
    end: datetime | None = None
    limit: int = 50
    offset: int = 0


@router.get(
    "/accounts/{account_id}/drafts", response=list[IgDraftOut], operation_id="instagram_drafts"
)
def list_drafts(
    request: HttpRequest, account_id: uuid.UUID, filters: Query[IgDraftFilters]
) -> list[IgDraftOut]:
    """Posts being planned, written, scheduled or published; the calendar is a date-ranged view."""
    account = account_of(request, account_id)
    rows = PostDraft.objects.filter(account=account)
    if filters.status:
        rows = rows.filter(status=filters.status)
    else:
        rows = rows.exclude(status=DraftStatus.CANCELLED)
    if filters.start:
        rows = rows.filter(scheduled_at__gte=filters.start)
    if filters.end:
        if filters.start and filters.end - filters.start > timedelta(days=MAX_RANGE_DAYS):
            raise ValidationFailedError("choose a range of at most 100 days")
        rows = rows.filter(scheduled_at__lt=filters.end)
    limit = max(1, min(filters.limit, MAX_PAGE))
    page = rows.order_by("scheduled_at", "-created_at")[
        max(0, filters.offset) : max(0, filters.offset) + limit
    ]
    return [draft_out(composer.refresh_status(d)) for d in page]


def _draft_of(request: HttpRequest, draft_id: uuid.UUID, permission: Permission) -> PostDraft:
    membership = resolve_workspace(request, permission)
    return drafts.get(membership.workspace_id, draft_id)


class IgDraftCreateIn(Schema):
    kind: str = "image"
    brief: str
    pillar: str = ""
    visuals: str = "text_cards"
    scheduled_at: datetime | None = None


@router.post(
    "/accounts/{account_id}/drafts", response=IgDraftOut, operation_id="instagram_draft_create"
)
def create_draft(request: HttpRequest, account_id: uuid.UUID, body: IgDraftCreateIn) -> IgDraftOut:
    """The AI writes a post (and draws its pictures) from a short brief."""
    account = account_of(request, account_id, Permission.EDIT_CONTENT)
    return draft_out(
        composer.create_draft(
            account,
            user_of(request).pk,
            kind=body.kind,
            brief=body.brief,
            pillar=body.pillar,
            visuals=body.visuals,
            scheduled_at=body.scheduled_at,
        )
    )


class IgManualDraftIn(Schema):
    kind: str = "image"
    caption: str = ""
    scheduled_at: datetime | None = None


@router.post(
    "/accounts/{account_id}/drafts/manual",
    response=IgDraftOut,
    operation_id="instagram_draft_manual",
)
def create_manual_draft(
    request: HttpRequest, account_id: uuid.UUID, body: IgManualDraftIn
) -> IgDraftOut:
    """An empty draft written by hand."""
    account = account_of(request, account_id, Permission.EDIT_CONTENT)
    draft = PostDraft.objects.create(
        workspace_id=account.workspace_id,
        account=account,
        kind=body.kind,
        status=DraftStatus.DRAFTED,
        caption=body.caption,
        scheduled_at=body.scheduled_at,
        origin="user",
        created_by=user_of(request),
    )
    return draft_out(drafts.update(draft, {}))


@router.get("/drafts/{draft_id}", response=IgDraftOut, operation_id="instagram_draft_get")
def get_draft(request: HttpRequest, draft_id: uuid.UUID) -> IgDraftOut:
    draft = composer.refresh_status(_draft_of(request, draft_id, Permission.VIEW))
    if draft.status in drafts.EDITABLE:
        draft.checks = checks.run_checks(draft)  # media or guardrails may have changed since
    return draft_out(draft)


class IgDraftUpdateIn(Schema):
    title: str | None = None
    caption: str | None = None
    hashtags: list[str] | None = None
    first_comment: str | None = None
    media: list[dict[str, Any]] | None = None
    cover_asset_id: uuid.UUID | None = None
    share_to_feed: bool | None = None
    location_id: str | None = None
    collaborators: list[str] | None = None
    scheduled_at: datetime | None = None
    pillar: str | None = None
    kind: str | None = None


@router.patch("/drafts/{draft_id}", response=IgDraftOut, operation_id="instagram_draft_update")
def update_draft(request: HttpRequest, draft_id: uuid.UUID, body: IgDraftUpdateIn) -> IgDraftOut:
    draft = _draft_of(request, draft_id, Permission.EDIT_CONTENT)
    return draft_out(drafts.update(draft, body.model_dump(exclude_unset=True)))


class IgReviseIn(Schema):
    feedback: str
    visuals: str | None = None


@router.post(
    "/drafts/{draft_id}/revise", response=IgDraftOut, operation_id="instagram_draft_revise"
)
def revise_draft(request: HttpRequest, draft_id: uuid.UUID, body: IgReviseIn) -> IgDraftOut:
    """The AI improves the draft following the feedback and keeps what works."""
    draft = _draft_of(request, draft_id, Permission.EDIT_CONTENT)
    return draft_out(composer.revise(draft, body.feedback, visuals=body.visuals))


class IgVisualsIn(Schema):
    mode: str = "text_cards"


@router.post(
    "/drafts/{draft_id}/visuals", response=IgDraftOut, operation_id="instagram_draft_visuals"
)
def make_visuals(request: HttpRequest, draft_id: uuid.UUID, body: IgVisualsIn) -> IgDraftOut:
    """Draw the pictures again, as text cards or with the connected image service."""
    draft = _draft_of(request, draft_id, Permission.EDIT_CONTENT)
    return draft_out(composer.regenerate_visuals(draft, user_of(request).pk, body.mode))


class IgApproveIn(Schema):
    scheduled_at: datetime | None = None


@router.post(
    "/drafts/{draft_id}/approve", response=IgDraftOut, operation_id="instagram_draft_approve"
)
def approve_draft(request: HttpRequest, draft_id: uuid.UUID, body: IgApproveIn) -> IgDraftOut:
    """Approve the post for publishing at the chosen time (now when empty)."""
    draft = _draft_of(request, draft_id, Permission.EDIT_CONTENT)
    if body.scheduled_at and body.scheduled_at < timezone.now() - timedelta(minutes=1):
        raise ValidationFailedError("choose a time in the future")
    return draft_out(drafts.approve(draft, user_of(request), body.scheduled_at))


@router.post(
    "/drafts/{draft_id}/unschedule", response=IgDraftOut, operation_id="instagram_draft_unschedule"
)
def unschedule_draft(request: HttpRequest, draft_id: uuid.UUID) -> IgDraftOut:
    draft = _draft_of(request, draft_id, Permission.EDIT_CONTENT)
    return draft_out(drafts.unschedule(draft))


@router.post(
    "/drafts/{draft_id}/cancel", response=IgDraftOut, operation_id="instagram_draft_cancel"
)
def cancel_draft(request: HttpRequest, draft_id: uuid.UUID) -> IgDraftOut:
    draft = _draft_of(request, draft_id, Permission.EDIT_CONTENT)
    return draft_out(drafts.cancel(draft))
