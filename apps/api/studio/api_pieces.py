"""Content studio API: posts (pieces), their calendar, approval and the hand-posting kit."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Schema

from core.errors import ValidationFailedError
from identity.authentication import api_auth
from studio import services, writer
from studio.api_schemas import StudioPieceOut, piece_out
from studio.api_scope import brand_scope
from studio.models import ContentPiece, PieceStatus
from workspaces.permissions import Permission

router = Router(tags=["studio-posts"], auth=api_auth)

MAX_RANGE_DAYS = 100
MAX_PAGE = 100


class StudioPieceFilters(Schema):
    status: str = ""
    channel: str = ""
    start: datetime | None = None
    end: datetime | None = None
    undated: bool = False
    limit: int = 50
    offset: int = 0


@router.get("/posts", response=list[StudioPieceOut], operation_id="studio_posts")
def list_posts(request: HttpRequest, filters: Query[StudioPieceFilters]) -> list[StudioPieceOut]:
    """Posts for the calendar (a date range), the backlog (undated) or one status."""
    scope = brand_scope(request, Permission.VIEW)
    rows = ContentPiece.objects.filter(brand_id=scope.brand.pk)
    rows = (
        rows.filter(status=filters.status)
        if filters.status
        else rows.exclude(status=PieceStatus.CANCELLED)
    )
    if filters.channel:
        rows = rows.filter(channel=filters.channel)
    if filters.undated:
        rows = rows.filter(scheduled_at__isnull=True)
    if filters.start:
        rows = rows.filter(scheduled_at__gte=filters.start)
    if filters.end:
        if filters.start and filters.end - filters.start > timedelta(days=MAX_RANGE_DAYS):
            raise ValidationFailedError("choose a range of at most 100 days")
        rows = rows.filter(scheduled_at__lt=filters.end)
    limit = max(1, min(filters.limit, MAX_PAGE))
    offset = max(0, filters.offset)
    return [
        piece_out(writer.refresh_status(p))
        for p in rows.order_by("scheduled_at", "-created_at")[offset : offset + limit]
    ]


def _piece(request: HttpRequest, piece_id: uuid.UUID, permission: Permission) -> ContentPiece:
    scope = brand_scope(request, permission)
    return services.get(scope.brand.pk, piece_id)


class StudioPieceCreateIn(Schema):
    channel: str
    kind: str
    brief: str
    pillar: str = ""
    scheduled_at: datetime | None = None


@router.post("/posts", response=StudioPieceOut, operation_id="studio_post_create")
def create_post(request: HttpRequest, body: StudioPieceCreateIn) -> StudioPieceOut:
    """The AI writes a post for one channel from a short brief."""
    scope = brand_scope(request, Permission.EDIT_CONTENT)
    return piece_out(
        writer.create(
            scope.brand,
            request.auth.pk,  # type: ignore[attr-defined]
            channel=body.channel,
            kind=body.kind,
            brief=body.brief,
            pillar=body.pillar,
            scheduled_at=body.scheduled_at,
        )
    )


@router.get("/posts/{piece_id}", response=StudioPieceOut, operation_id="studio_post_get")
def get_post(request: HttpRequest, piece_id: uuid.UUID) -> StudioPieceOut:
    piece = writer.refresh_status(_piece(request, piece_id, Permission.VIEW))
    if piece.status in services.EDITABLE:
        piece.checks = writer.run_checks(piece)  # media or settings may have changed since
    return piece_out(piece)


class StudioPieceUpdateIn(Schema):
    title: str | None = None
    body: str | None = None
    hashtags: list[str] | None = None
    first_comment: str | None = None
    slides: list[dict[str, Any]] | None = None
    media: list[dict[str, Any]] | None = None
    cover_asset_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None
    pillar: str | None = None


@router.patch("/posts/{piece_id}", response=StudioPieceOut, operation_id="studio_post_update")
def update_post(
    request: HttpRequest, piece_id: uuid.UUID, body: StudioPieceUpdateIn
) -> StudioPieceOut:
    piece = _piece(request, piece_id, Permission.EDIT_CONTENT)
    return piece_out(services.update(piece, body.model_dump(exclude_unset=True)))


class StudioReviseIn(Schema):
    feedback: str


@router.post("/posts/{piece_id}/revise", response=StudioPieceOut, operation_id="studio_post_revise")
def revise_post(request: HttpRequest, piece_id: uuid.UUID, body: StudioReviseIn) -> StudioPieceOut:
    """The AI improves the post following the feedback and keeps what works."""
    piece = _piece(request, piece_id, Permission.EDIT_CONTENT)
    return piece_out(writer.revise(piece, body.feedback))


class StudioRedrawIn(Schema):
    pass


@router.post("/posts/{piece_id}/redraw", response=StudioPieceOut, operation_id="studio_post_redraw")
def redraw_post(request: HttpRequest, piece_id: uuid.UUID, body: StudioRedrawIn) -> StudioPieceOut:
    """Make the pictures again from the current text."""
    piece = _piece(request, piece_id, Permission.EDIT_CONTENT)
    return piece_out(writer.redraw(piece, request.auth.pk))  # type: ignore[attr-defined]


class StudioApproveIn(Schema):
    scheduled_at: datetime | None = None


@router.post(
    "/posts/{piece_id}/approve", response=StudioPieceOut, operation_id="studio_post_approve"
)
def approve_post(
    request: HttpRequest, piece_id: uuid.UUID, body: StudioApproveIn
) -> StudioPieceOut:
    """Approve the post for its time (the chosen one, else its slot, else now)."""
    piece = _piece(request, piece_id, Permission.EDIT_CONTENT)
    if body.scheduled_at and body.scheduled_at < timezone.now() - timedelta(minutes=1):
        raise ValidationFailedError("choose a time in the future")
    return piece_out(services.approve(piece, request.auth, body.scheduled_at))  # type: ignore[attr-defined]


@router.post(
    "/posts/{piece_id}/unschedule", response=StudioPieceOut, operation_id="studio_post_unschedule"
)
def unschedule_post(request: HttpRequest, piece_id: uuid.UUID) -> StudioPieceOut:
    return piece_out(services.unschedule(_piece(request, piece_id, Permission.EDIT_CONTENT)))


@router.post("/posts/{piece_id}/cancel", response=StudioPieceOut, operation_id="studio_post_cancel")
def cancel_post(request: HttpRequest, piece_id: uuid.UUID) -> StudioPieceOut:
    return piece_out(services.cancel(_piece(request, piece_id, Permission.EDIT_CONTENT)))


class StudioPostedIn(Schema):
    url: str = ""


@router.post(
    "/posts/{piece_id}/mark-posted", response=StudioPieceOut, operation_id="studio_post_mark_posted"
)
def mark_posted(request: HttpRequest, piece_id: uuid.UUID, body: StudioPostedIn) -> StudioPieceOut:
    """Tell the studio you posted it by hand."""
    return piece_out(
        services.mark_posted(_piece(request, piece_id, Permission.EDIT_CONTENT), body.url)
    )


class StudioKitOut(Schema):
    text: str
    first_comment: str
    media: list[dict[str, Any]]
    cover_asset_id: str
    scheduled_at: datetime | None


@router.get("/posts/{piece_id}/kit", response=StudioKitOut, operation_id="studio_post_kit")
def post_kit(request: HttpRequest, piece_id: uuid.UUID) -> StudioKitOut:
    """The text to paste and the files to upload when posting by hand."""
    return StudioKitOut(**services.kit(_piece(request, piece_id, Permission.VIEW)))
