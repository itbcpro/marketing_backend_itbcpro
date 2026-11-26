"""Action kind for replacing the HTML body of a post with an AI rewrite the owner approved.

R2: a person approves it. The executor reads the live post first, refuses when someone edited it
since the rewrite was made or when the post is built with a page builder, keeps the original body
so the change can be rolled back exactly, and verifies the saved result.
"""

import re
import uuid
from contextlib import suppress
from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from actions.models import ProposedAction, RiskClass
from actions.registry import ActionKind, register_action_kind
from content import body_html
from content.models import ContentItem, ItemSource, PostRevision, RevisionStatus
from content.sync import parse_gmt, refresh_item, rest_base_for
from content.wordpress import factory
from core.errors import DomainError, NotFoundError, PermanentProviderError

KIND = "content.update_post_body"
CLOCK_SKEW = timedelta(seconds=2)
WORD_TOLERANCE = 0.02
SPACES = re.compile(r"\s+")


class UpdatePostBody(BaseModel):
    website_id: uuid.UUID
    item_id: uuid.UUID
    revision_id: uuid.UUID | None = None
    html: str = ""
    """The body to save. Empty means: the rewrite of ``revision_id``. A rollback sets it."""
    base_modified_at: str = ""


def _item(action: ProposedAction, payload: UpdatePostBody) -> ContentItem:
    item = (
        ContentItem.objects.select_related("website__connection")
        .filter(
            pk=payload.item_id,
            website_id=payload.website_id,
            workspace_id=action.workspace_id,
            source=ItemSource.WORDPRESS,
        )
        .first()
    )
    if item is None or item.website.connection is None:
        raise NotFoundError("this post cannot be changed through WordPress")
    return item


def _revision(action: ProposedAction, payload: UpdatePostBody) -> PostRevision | None:
    if payload.revision_id is None:
        return None
    return PostRevision.objects.filter(
        pk=payload.revision_id, workspace_id=action.workspace_id
    ).first()


def _new_html(action: ProposedAction, payload: UpdatePostBody) -> str:
    if payload.html:
        return payload.html
    revision = _revision(action, payload)
    if revision is None or not revision.after_html:
        raise NotFoundError("the rewrite is no longer available")
    return revision.after_html


def _check_unchanged(live: dict[str, Any], payload: UpdatePostBody) -> None:
    base = parse_gmt(payload.base_modified_at)
    now = parse_gmt(live.get("modified_gmt"))
    if base is not None and now is not None and now > base + CLOCK_SKEW:
        raise PermanentProviderError("page_changed_on_site", provider="wordpress")
    if body_html.is_builder_post(live):
        raise PermanentProviderError("page_builder_post", provider="wordpress")


def execute(action: ProposedAction) -> dict[str, Any]:
    payload = UpdatePostBody.model_validate(action.effective_payload)
    item = _item(action, payload)
    html = _new_html(action, payload)
    client = factory.for_connection(item.website.connection)  # type: ignore[arg-type]
    base = rest_base_for(client, item)
    live = client.item_body(base, int(item.external_id))
    _check_unchanged(live, payload)
    before = str((live.get("content") or {}).get("raw", ""))
    saved = client.update_item(base, int(item.external_id), {"content": html})
    with suppress(DomainError):
        refresh_item(item)  # score, headings and links are read again
    revision = _revision(action, payload)
    if revision is not None and not payload.html:
        revision.status = RevisionStatus.APPLIED
        revision.save(update_fields=["status", "updated_at"])
    return {
        "before": {"body": before},
        "after": {"words": body_html.word_count(html)},
        "modified_gmt": str(saved.get("modified_gmt", "")),
    }


def _squash(text: str) -> str:
    return SPACES.sub(" ", text).strip()


def verify(action: ProposedAction) -> bool:
    payload = UpdatePostBody.model_validate(action.effective_payload)
    item = _item(action, payload)
    html = _new_html(action, payload)
    client = factory.for_connection(item.website.connection)  # type: ignore[arg-type]
    live = client.item_body(rest_base_for(client, item), int(item.external_id))
    saved = str((live.get("content") or {}).get("raw", ""))
    if _squash(saved) == _squash(html):
        return True
    wanted, found = body_html.word_count(html), body_html.word_count(saved)
    return abs(wanted - found) <= max(3, WORD_TOLERANCE * wanted) and (
        body_html.links_of(html) <= body_html.links_of(saved)
    )


def preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = UpdatePostBody.model_validate(action.payload)
    revision = _revision(action, payload)
    if revision is None:
        return None, {"words": body_html.word_count(payload.html)}
    stats = revision.stats or {}
    return {"words": stats.get("before_words", 0)}, {"words": stats.get("after_words", 0)}


def rollback_payload(action: ProposedAction) -> dict[str, Any]:
    payload = UpdatePostBody.model_validate(action.effective_payload)
    before = str(((action.result or {}).get("before") or {}).get("body", ""))
    return {
        "website_id": str(payload.website_id),
        "item_id": str(payload.item_id),
        "html": before,
        "base_modified_at": str((action.result or {}).get("modified_gmt", "")),
    }


def register_action_kinds() -> None:
    register_action_kind(
        ActionKind(
            kind=KIND,
            payload_model=UpdatePostBody,
            risk=RiskClass.R2,
            executor=execute,
            description="Replace the body of a post with an AI rewrite the owner approved.",
            preview=preview,
            verify=verify,
            rollback_kind=KIND,
            rollback_payload=rollback_payload,
        )
    )
