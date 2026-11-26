"""Bulk actions on conversations: a registry of small functions, so a new action is a new entry."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings

from audit.actors import Actor
from core.errors import ValidationFailedError
from crm.inbox import conversations
from crm.inbox.models import ConversationStatus
from workspaces.models import Membership
from workspaces.permissions import Permission, role_allows


@dataclass(frozen=True)
class Context:
    membership: Membership
    actor: Actor
    conversation_id: uuid.UUID
    user_id: uuid.UUID | None


@dataclass(frozen=True)
class BulkAction:
    run: Callable[[Context], None]
    needs_edit: bool = True
    needs_user: bool = False


def _read(c: Context) -> None:
    conversations.mark_read(
        workspace_id=c.membership.workspace_id,
        conversation_id=c.conversation_id,
        user_id=c.membership.user_id,
    )


def _unread(c: Context) -> None:
    conversations.mark_unread(
        workspace_id=c.membership.workspace_id,
        conversation_id=c.conversation_id,
        user_id=c.membership.user_id,
    )


def _flags(starred: bool | None = None, status: str | None = None) -> Callable[[Context], None]:
    def run(c: Context) -> None:
        conversations.set_flags(
            workspace_id=c.membership.workspace_id,
            conversation_id=c.conversation_id,
            starred=starred,
            status=status,
        )

    return run


def _assign(c: Context) -> None:
    conversations.assign(
        workspace_id=c.membership.workspace_id,
        actor=c.actor,
        conversation_id=c.conversation_id,
        user_id=c.user_id,
    )


ACTIONS: dict[str, BulkAction] = {
    "read": BulkAction(_read, needs_edit=False),
    "unread": BulkAction(_unread, needs_edit=False),
    "star": BulkAction(_flags(starred=True)),
    "unstar": BulkAction(_flags(starred=False)),
    "close": BulkAction(_flags(status=ConversationStatus.CLOSED)),
    "reopen": BulkAction(_flags(status=ConversationStatus.OPEN)),
    "assign": BulkAction(_assign, needs_user=True),
    "unassign": BulkAction(_assign),
}


def run(
    membership: Membership,
    actor: Actor,
    *,
    action: str,
    visible_ids: list[uuid.UUID],
    requested: int,
    user_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Apply ``action`` to the conversations the member may see; the rest are counted as skipped."""
    chosen = ACTIONS.get(action)
    if chosen is None:
        raise ValidationFailedError("invalid action", errors={"action": "invalid_value"})
    if requested > settings.CRM_INBOX_BULK_LIMIT:
        raise ValidationFailedError("too many", errors={"ids": "too_many"})
    if chosen.needs_user and user_id is None:
        raise ValidationFailedError("user required", errors={"user_id": "required"})
    for pk in visible_ids:
        chosen.run(Context(membership, actor, pk, user_id))
    return {"done": len(visible_ids), "skipped": requested - len(visible_ids)}


def allowed(membership: Membership, action: str) -> bool:
    chosen = ACTIONS.get(action)
    return chosen is not None and (
        not chosen.needs_edit or role_allows(membership.role, Permission.CRM_EDIT)
    )
