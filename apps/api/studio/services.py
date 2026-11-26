"""Piece workflow: edit, approve, hand over to a publisher or to a person, cancel, mark posted.

Approval is a person's decision (or, for a channel set to auto, the decision of the person who
enabled automation, re-checked at the moment of use). Approving a piece for a channel with a
connected publisher hands it over there; otherwise it becomes a kit that is due at its time.
"""

import uuid
from datetime import datetime
from typing import Any

from django.utils import timezone

from core.errors import ConflictError, DomainError, NotFoundError, ValidationFailedError
from core.logging import get_logger
from identity.models import User
from studio import config_store, writer
from studio.models import ContentPiece, PieceStatus
from studio.publishers.ports import account_for, publisher_for

log = get_logger(__name__)

EDITABLE = {PieceStatus.DRAFT, PieceStatus.FAILED, PieceStatus.SCHEDULED}
COPY_FIELDS = {"body", "hashtags", "first_comment", "slides", "cta"}
PIECE_FIELDS = {"title", "media", "cover_asset_id", "scheduled_at", "pillar"}
OPEN_FOR_APPROVAL = {PieceStatus.DRAFT, PieceStatus.FAILED}
OUTSTANDING = (PieceStatus.SCHEDULED, PieceStatus.PUBLISHING)


def get(brand_id: uuid.UUID, piece_id: uuid.UUID) -> ContentPiece:
    found = (
        ContentPiece.objects.select_related("idea").filter(pk=piece_id, brand_id=brand_id).first()
    )
    if found is None:
        raise NotFoundError("post not found")
    return found


def _withdraw(piece: ContentPiece) -> None:
    publisher = publisher_for(piece.channel)
    if publisher is not None and piece.external.get("draft_id"):
        publisher.withdraw(piece)
    piece.external = {}


def _back_to_draft(piece: ContentPiece) -> None:
    if piece.status == PieceStatus.SCHEDULED:
        _withdraw(piece)
        piece.status = PieceStatus.DRAFT
    elif piece.status == PieceStatus.FAILED:
        piece.status = PieceStatus.DRAFT
        piece.error_code = piece.error_detail = ""
    piece.approved_by = None
    piece.approved_at = None


def update(piece: ContentPiece, changes: dict[str, Any]) -> ContentPiece:
    if piece.status not in EDITABLE:
        raise ConflictError("this post can no longer be changed")
    unknown = set(changes) - COPY_FIELDS - PIECE_FIELDS
    if unknown:
        raise ValidationFailedError(f"cannot change: {', '.join(sorted(unknown))}")
    piece.copy = {**piece.copy, **{k: v for k, v in changes.items() if k in COPY_FIELDS}}
    for field in PIECE_FIELDS & set(changes):
        setattr(piece, field, changes[field])
    _back_to_draft(piece)
    piece.checks = writer.run_checks(piece)
    piece.save()
    return piece


def _publisher_target(piece: ContentPiece) -> tuple[Any, str] | None:
    plan = config_store.load(piece.brand_id).plan_for(piece.channel)
    if plan.publish_mode == "manual":
        return None
    account = account_for(piece.workspace_id, piece.channel, plan.account_id)
    publisher = publisher_for(piece.channel)
    return (publisher, account.id) if publisher and account else None


def approve(piece: ContentPiece, user: User, scheduled_at: datetime | None = None) -> ContentPiece:
    """Approve for posting at ``scheduled_at`` (default: its slot, else now)."""
    from workspaces.permissions import Permission
    from workspaces.services import require_permission

    require_permission(user, piece.workspace_id, Permission.APPROVE_R2)
    if piece.status not in OPEN_FOR_APPROVAL:
        raise ConflictError("this post cannot be approved in its current state")
    results = writer.run_checks(piece)
    piece.checks = results
    if any(r["severity"] == "error" for r in results):
        piece.save(update_fields=["checks", "updated_at"])
        raise ValidationFailedError("fix the problems marked as errors first")
    piece.scheduled_at = scheduled_at or piece.scheduled_at or timezone.now()
    target = _publisher_target(piece)
    piece.external = {}
    if target is not None:
        publisher, account_id = target
        piece.external = publisher.hand_over(piece, user, account_id).external
    else:
        piece.external = {"manual": True}
    piece.status = PieceStatus.SCHEDULED
    piece.approved_by = user
    piece.approved_at = timezone.now()
    piece.error_code = piece.error_detail = ""
    piece.save()
    return piece


def unschedule(piece: ContentPiece) -> ContentPiece:
    if piece.status != PieceStatus.SCHEDULED:
        raise ConflictError("only a scheduled post can be taken back")
    _back_to_draft(piece)
    piece.save()
    return piece


def cancel(piece: ContentPiece) -> ContentPiece:
    if piece.status in {PieceStatus.PUBLISHING, PieceStatus.PUBLISHED}:
        raise ConflictError("this post is already going out")
    if piece.status == PieceStatus.SCHEDULED:
        _withdraw(piece)
    piece.status = PieceStatus.CANCELLED
    piece.save()
    return piece


def mark_posted(piece: ContentPiece, url: str = "") -> ContentPiece:
    """A person posted the kit by hand."""
    if piece.status not in {PieceStatus.MANUAL_DUE, PieceStatus.SCHEDULED, PieceStatus.DRAFT}:
        raise ConflictError("this post cannot be marked as posted")
    piece.status = PieceStatus.PUBLISHED
    piece.published_at = timezone.now()
    piece.permalink = url.strip()[:500]
    piece.save()
    return piece


def kit(piece: ContentPiece) -> dict[str, Any]:
    """Everything needed to post by hand: the text as it should be pasted, and the files."""
    body = str(piece.copy.get("body", "")).strip()
    tags = " ".join(str(t) for t in piece.copy.get("hashtags", []) if t and t not in body)
    return {
        "text": f"{body}\n\n{tags}".strip() if tags else body,
        "first_comment": str(piece.copy.get("first_comment", "")),
        "media": piece.media,
        "cover_asset_id": str(piece.cover_asset_id) if piece.cover_asset_id else "",
        "scheduled_at": piece.scheduled_at,
    }


def dispatch_due() -> dict[str, int]:
    """Pieces approved for hand posting whose time has come become 'due'. Safe to run often."""
    counts = {"due": 0}
    rows = ContentPiece.objects.filter(
        status=PieceStatus.SCHEDULED, scheduled_at__lte=timezone.now(), external__manual=True
    )
    for piece in rows[:200]:
        piece.status = PieceStatus.MANUAL_DUE
        piece.save(update_fields=["status", "updated_at"])
        counts["due"] += 1
    return counts


def sync_external() -> dict[str, int]:
    """Copy the state of handed-over pieces from their publisher (published, failed, taken back)."""
    counts = {"checked": 0, "changed": 0}
    rows = (
        ContentPiece.objects.filter(status__in=OUTSTANDING)
        .exclude(external={})
        .exclude(external__has_key="manual")
    )
    for piece in rows[:200]:
        publisher = publisher_for(piece.channel)
        if publisher is None:
            continue
        counts["checked"] += 1
        try:
            update_ = publisher.refresh(piece)
        except DomainError:
            continue
        if update_ is None or update_.status == piece.status:
            continue
        piece.status = update_.status
        piece.permalink = update_.permalink or piece.permalink
        piece.published_at = update_.published_at or piece.published_at
        piece.error_code, piece.error_detail = update_.error_code, update_.error_detail
        if update_.status == PieceStatus.DRAFT:
            _back_to_draft(piece)
        piece.save()
        counts["changed"] += 1
    return counts
