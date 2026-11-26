"""AI body rewrite of a post: start it, read the result, apply or discard it."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from actions import services as action_services
from actions.models import ActionStatus
from content import body_html, improve
from content.api_pages import PgApplied
from content.body_action import KIND, UpdatePostBody
from content.models import ContentItem, PostRevision, RevisionStatus
from content.tasks import improve_post
from core.errors import NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["websites"], auth=api_auth)

RUNNING = {RevisionStatus.QUEUED, RevisionStatus.RUNNING}


class ImproveIn(Schema):
    instruction: str = ""
    focus: list[str] = []


class RevisionOut(Schema):
    id: uuid.UUID
    status: str
    instruction: str
    focus: list[str]
    changes: list[dict[str, Any]]
    notes: str
    stats: dict[str, Any]
    error: str
    created_at: datetime
    finished_at: datetime | None
    before_preview: str
    after_preview: str


def _item(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, permission: Permission
) -> ContentItem:
    membership = resolve_workspace(request, permission)
    item = (
        ContentItem.objects.select_related("website__connection")
        .filter(pk=item_id, website_id=website_id, workspace_id=membership.workspace_id)
        .first()
    )
    if item is None:
        raise NotFoundError("page not found")
    return item


def _out(revision: PostRevision) -> dict[str, Any]:
    return {
        "id": revision.pk,
        "status": revision.status,
        "instruction": revision.instruction,
        "focus": revision.focus,
        "changes": revision.changes,
        "notes": revision.notes,
        "stats": revision.stats,
        "error": revision.error,
        "created_at": revision.created_at,
        "finished_at": revision.finished_at,
        "before_preview": body_html.clean_html(revision.before_html),
        "after_preview": revision.after_html,
    }


def _latest(item: ContentItem) -> PostRevision | None:
    return item.revisions.exclude(status=RevisionStatus.DISMISSED).first()


@router.post(
    "/{website_id}/pages/{item_id}/improve-body",
    response=RevisionOut,
    operation_id="website_page_improve_body",
)
def improve_body(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, payload: ImproveIn
) -> dict[str, Any]:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    running = item.revisions.filter(status__in=RUNNING).first()
    if running is not None:
        return _out(running)
    revision = improve.start(
        item,
        requested_by=f"user:{user.pk}",
        instruction=payload.instruction,
        focus=payload.focus,
    )
    improve_post.delay(str(revision.pk))
    return _out(revision)


@router.get(
    "/{website_id}/pages/{item_id}/revision",
    response=RevisionOut | None,
    operation_id="website_page_revision",
)
def latest_revision(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID
) -> dict[str, Any] | None:
    revision = _latest(_item(request, website_id, item_id, Permission.VIEW))
    return _out(revision) if revision else None


def _revision(item: ContentItem, revision_id: uuid.UUID) -> PostRevision:
    revision = item.revisions.filter(pk=revision_id).first()
    if revision is None:
        raise NotFoundError("rewrite not found")
    return revision


@router.post(
    "/{website_id}/pages/{item_id}/revision/{revision_id}/apply",
    response=PgApplied,
    operation_id="website_page_revision_apply",
)
def apply_revision(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, revision_id: uuid.UUID
) -> PgApplied:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    revision = _revision(item, revision_id)
    if revision.status != RevisionStatus.DONE:
        raise ValidationFailedError("this rewrite cannot be applied")
    user: User = request.auth  # type: ignore[attr-defined]
    spec = UpdatePostBody(
        website_id=website_id,
        item_id=item.pk,
        revision_id=revision.pk,
        base_modified_at=revision.base_modified_at,
    )
    action = action_services.propose(
        workspace_id=item.workspace_id,
        kind=KIND,
        payload=spec.model_dump(mode="json"),
        requested_by=f"user:{user.pk}",
        summary=f"Improve the body of {item.title or item.url}"[:300],
        idempotency_key=f"body:{revision.pk}:{uuid.uuid4()}",
    )
    if action.status == ActionStatus.AWAITING_APPROVAL:
        action = action_services.approve(
            workspace_id=item.workspace_id, action_id=action.pk, user=user
        )
    return PgApplied(action_id=action.pk, status=action.status)


@router.post(
    "/{website_id}/pages/{item_id}/revision/{revision_id}/dismiss",
    response=RevisionOut,
    operation_id="website_page_revision_dismiss",
)
def dismiss_revision(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, revision_id: uuid.UUID
) -> dict[str, Any]:
    item = _item(request, website_id, item_id, Permission.EDIT_CONTENT)
    revision = _revision(item, revision_id)
    if revision.status in RUNNING:
        raise ValidationFailedError("wait until the rewrite has finished")
    if revision.status != RevisionStatus.APPLIED:
        revision.status = RevisionStatus.DISMISSED
        revision.save(update_fields=["status", "updated_at"])
    return _out(revision)
