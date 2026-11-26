"""Post workflow: edit, check, approve, schedule and hand over to the publishing action.

A post is approved by a person who may approve public posts (R2). The approval is recorded on the
draft; at the scheduled time the platform proposes the publish action and approves it on that
person's behalf, so the same guardrails, kill switch, audit and verification apply as for every
other action. A slot missed by hours is not published late; it is failed with a clear reason.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from actions import guardrails
from actions import services as action_services
from core.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from core.logging import get_logger
from identity.models import User
from social import changes, checks
from social.models import DraftStatus, PostDraft
from social.response_settings import PREFIX
from social.schemas import PublishAsset, PublishPost

log = get_logger(__name__)

PUBLISH_KIND = PREFIX + "publish_post"
MISSED_AFTER = timedelta(hours=6)
EDITABLE = {DraftStatus.IDEA, DraftStatus.DRAFTED, DraftStatus.FAILED, DraftStatus.SCHEDULED}
EDIT_FIELDS = {
    "title", "caption", "hashtags", "first_comment", "media", "cover_asset_id", "share_to_feed",
    "location_id", "collaborators", "scheduled_at", "pillar", "kind",
}  # fmt: skip
MAX_COLLABORATORS = 3


def get(workspace_id: uuid.UUID, draft_id: uuid.UUID) -> PostDraft:
    found = (
        PostDraft.objects.select_related("account")
        .filter(pk=draft_id, workspace_id=workspace_id)
        .first()
    )
    if found is None:
        raise NotFoundError("post not found")
    return found


def full_caption(draft: PostDraft) -> str:
    caption = draft.caption.strip()
    tags = " ".join(t for t in draft.hashtags if t and t not in caption)
    return f"{caption}\n\n{tags}".strip() if tags else caption


def publish_payload(draft: PostDraft) -> dict[str, Any]:
    media = [
        PublishAsset(asset_id=uuid.UUID(str(m["asset_id"])), alt_text=str(m.get("alt_text") or ""))
        for m in draft.media
    ]
    model = PublishPost(
        connection_id=draft.account.connection_id,
        draft_id=draft.pk,
        kind=draft.kind,  # type: ignore[arg-type]
        caption=full_caption(draft),
        media=media,
        cover_asset_id=draft.cover_asset_id,
        share_to_feed=draft.share_to_feed,
        location_id=draft.location_id,
        collaborators=[str(c) for c in draft.collaborators],
        first_comment=draft.first_comment,
    )
    return model.model_dump(mode="json")


def update(draft: PostDraft, changes_: dict[str, Any]) -> PostDraft:
    if draft.status not in EDITABLE:
        raise ConflictError("this post can no longer be changed")
    unknown = set(changes_) - EDIT_FIELDS
    if unknown:
        raise ValidationFailedError(f"cannot change: {', '.join(sorted(unknown))}")
    if len(changes_.get("collaborators", [])) > MAX_COLLABORATORS:
        raise ValidationFailedError("a post can have at most 3 collaborators")
    for field, value in changes_.items():
        setattr(draft, field, value)
    if draft.status == DraftStatus.SCHEDULED:
        draft.status = DraftStatus.DRAFTED  # an edited post must be approved again
        draft.approved_by = None
        draft.approved_at = None
    elif draft.status == DraftStatus.FAILED:
        draft.status = DraftStatus.DRAFTED
        draft.error_code = draft.error_detail = ""
    draft.checks = checks.run_checks(draft)
    draft.save()
    return draft


def approve(draft: PostDraft, user: User, scheduled_at: datetime | None) -> PostDraft:
    """Approve for publishing at ``scheduled_at`` (or now); needs the right to approve posts."""
    from workspaces.permissions import Permission
    from workspaces.services import require_permission

    require_permission(user, draft.workspace_id, Permission.APPROVE_R2)
    if draft.status not in {DraftStatus.IDEA, DraftStatus.DRAFTED, DraftStatus.FAILED}:
        raise ConflictError("this post cannot be approved in its current state")
    results = checks.run_checks(draft)
    draft.checks = results
    if checks.has_errors(results):
        draft.save(update_fields=["checks", "updated_at"])
        raise ValidationFailedError("fix the problems marked as errors first")
    when = scheduled_at or timezone.now()
    draft.scheduled_at = when
    draft.status = DraftStatus.SCHEDULED
    draft.approved_by = user
    draft.approved_at = timezone.now()
    draft.error_code = draft.error_detail = ""
    draft.publish_state = {}
    draft.save()
    if when <= timezone.now() + timedelta(minutes=1):
        _publish_soon()
    return draft


def _publish_soon() -> None:
    from social.tasks import publish_due_task

    transaction.on_commit(publish_due_task.delay)


def unschedule(draft: PostDraft) -> PostDraft:
    if draft.status != DraftStatus.SCHEDULED:
        raise ConflictError("only a scheduled post can be taken back")
    draft.status = DraftStatus.DRAFTED
    draft.approved_by = None
    draft.approved_at = None
    draft.save()
    return draft


def cancel(draft: PostDraft) -> PostDraft:
    if draft.status in {DraftStatus.PUBLISHING, DraftStatus.PUBLISHED}:
        raise ConflictError("this post is already going out")
    draft.status = DraftStatus.CANCELLED
    draft.save()
    return draft


def _fail(draft: PostDraft, code: str, detail: str) -> None:
    PostDraft.objects.filter(pk=draft.pk).update(
        status=DraftStatus.FAILED,
        error_code=code,
        error_detail=detail[:500],
        updated_at=timezone.now(),
    )


def _launch(draft: PostDraft) -> bool:
    approver = draft.approved_by
    if approver is None:
        _fail(draft, "not_approved", "the post has no approver")
        return False
    stamp = int(draft.approved_at.timestamp()) if draft.approved_at else 0
    action = action_services.propose(
        workspace_id=draft.workspace_id,
        kind=PUBLISH_KIND,
        payload=publish_payload(draft),
        requested_by=f"user:{approver.pk}",
        summary=f"Publish {draft.kind}: {draft.title or draft.caption[:60]}"[:300],
        idempotency_key=f"publish-{draft.pk}-{stamp}",
    )
    try:
        action = changes.approve_as(action, approver)
    except PermissionDeniedError:
        _fail(draft, "permission_denied", "the approver can no longer approve posts")
        return False
    PostDraft.objects.filter(pk=draft.pk).update(
        status=DraftStatus.PUBLISHING, action_id=action.pk, updated_at=timezone.now()
    )
    return True


def publish_due() -> dict[str, int]:
    """Launch every approved post whose time has come. Safe to run every minute."""
    now = timezone.now()
    counts = {"launched": 0, "missed": 0, "paused": 0, "failed": 0}
    due = PostDraft.objects.select_related("account", "approved_by").filter(
        status=DraftStatus.SCHEDULED, scheduled_at__lte=now
    )
    for draft in due[:50]:
        if guardrails.kill_switch_active(draft.workspace_id):
            counts["paused"] += 1
            continue
        if draft.scheduled_at and now - draft.scheduled_at > MISSED_AFTER:
            _fail(draft, "missed_slot", "the scheduled time passed while publishing was paused")
            counts["missed"] += 1
            continue
        try:
            counts["launched" if _launch(draft) else "failed"] += 1
        except DomainError as exc:
            _fail(draft, exc.code.value, str(exc))
            counts["failed"] += 1
    return counts


def calendar(account_id: uuid.UUID, start: datetime, end: datetime) -> list[PostDraft]:
    return list(
        PostDraft.objects.filter(
            account_id=account_id, scheduled_at__gte=start, scheduled_at__lt=end
        )
        .exclude(status=DraftStatus.CANCELLED)
        .order_by("scheduled_at")
    )
