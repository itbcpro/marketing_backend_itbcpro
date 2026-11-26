"""Bulk actions on contacts: a registry of small functions, so a new action is a new entry."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings

from audit.actors import Actor
from core.errors import ValidationFailedError
from crm.contacts import contact_service, tags
from workspaces.models import Membership
from workspaces.permissions import Permission, role_allows


@dataclass(frozen=True)
class Context:
    membership: Membership
    actor: Actor
    contact_id: uuid.UUID
    user_id: uuid.UUID | None
    names: list[str]


@dataclass(frozen=True)
class BulkAction:
    run: Callable[[Context], None]
    permission: Permission = Permission.CRM_EDIT
    needs_names: bool = False
    needs_user: bool = False


def _add_tags(c: Context) -> None:
    contact = contact_service.get_contact(c.membership.workspace_id, c.contact_id)
    tags.add_tags(
        workspace_id=c.membership.workspace_id, actor=c.actor, contact=contact, names=c.names
    )


def _remove_tags(c: Context) -> None:
    contact = contact_service.get_contact(c.membership.workspace_id, c.contact_id)
    tags.remove_tags(
        workspace_id=c.membership.workspace_id, actor=c.actor, contact=contact, names=c.names
    )


def _assign(c: Context) -> None:
    contact_service.assign(
        workspace_id=c.membership.workspace_id,
        actor=c.actor,
        contact_id=c.contact_id,
        user_id=c.user_id,
    )


def _delete(c: Context) -> None:
    contact_service.delete_contact(
        workspace_id=c.membership.workspace_id, actor=c.actor, contact_id=c.contact_id
    )


ACTIONS: dict[str, BulkAction] = {
    "add_tags": BulkAction(_add_tags, needs_names=True),
    "remove_tags": BulkAction(_remove_tags, needs_names=True),
    "assign": BulkAction(_assign, needs_user=True),
    "unassign": BulkAction(_assign),
    "delete": BulkAction(_delete, permission=Permission.CRM_DELETE),
}


def allowed(membership: Membership, action: str) -> bool:
    chosen = ACTIONS.get(action)
    return chosen is not None and role_allows(membership.role, chosen.permission)


def run(
    membership: Membership,
    actor: Actor,
    *,
    action: str,
    visible_ids: list[uuid.UUID],
    requested: int,
    user_id: uuid.UUID | None = None,
    names: list[str] | None = None,
) -> dict[str, int]:
    """Apply ``action`` to the contacts the member may see; the rest are counted as skipped."""
    chosen = ACTIONS.get(action)
    if chosen is None:
        raise ValidationFailedError("invalid action", errors={"action": "invalid_value"})
    if requested > settings.CRM_CONTACTS_BULK_LIMIT:
        raise ValidationFailedError("too many", errors={"ids": "too_many"})
    if chosen.needs_user and user_id is None:
        raise ValidationFailedError("user required", errors={"user_id": "required"})
    if chosen.needs_names and not names:
        raise ValidationFailedError("names required", errors={"names": "required"})
    for pk in visible_ids:
        chosen.run(Context(membership, actor, pk, user_id, names or []))
    return {"done": len(visible_ids), "skipped": requested - len(visible_ids)}
