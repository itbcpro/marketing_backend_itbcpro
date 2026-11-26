"""Smart lists: saved dynamic filters."""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Q, QuerySet

from audit.actors import Actor
from audit.services import record_event
from core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from crm.contacts import services as contacts
from crm.contacts.models import Contact
from crm.lists.models import SmartList, Visibility
from workspaces.models import Membership

NAME_LIMIT = 100
MAX_COLUMNS = 30


def _check(workspace_id: uuid.UUID, name: str, spec: dict[str, Any], columns: list[str]) -> str:
    clean = name.strip()
    if not clean or len(clean) > NAME_LIMIT:
        raise ValidationFailedError(
            "invalid name", errors={"name": "required" if not clean else "too_long"}
        )
    if len(columns) > MAX_COLUMNS or not all(isinstance(c, str) for c in columns):
        raise ValidationFailedError("invalid columns", errors={"columns": "invalid_value"})
    # Compiling proves the filter is valid now; it is compiled again on every evaluation.
    contacts.compile_filter(workspace_id, spec)
    return clean


def visible_lists(membership: Membership) -> QuerySet[SmartList]:
    return SmartList.objects.for_workspace(membership.workspace_id).filter(
        Q(visibility=Visibility.SHARED) | Q(owner_id=membership.user_id)
    )


def create(
    *,
    membership: Membership,
    actor: Actor,
    name: str,
    spec: dict[str, Any],
    columns: list[str],
    visibility: str = Visibility.PRIVATE,
) -> SmartList:
    if visibility not in Visibility.values:
        raise ValidationFailedError("invalid visibility", errors={"visibility": "invalid_value"})
    clean = _check(membership.workspace_id, name, spec, columns)
    row = SmartList.objects.create(
        workspace_id=membership.workspace_id,
        name=clean,
        filter=spec,
        columns=columns,
        visibility=visibility,
        owner_id=membership.user_id,
    )
    record_event(
        action="crm.smart_list.created",
        actor=actor,
        workspace_id=membership.workspace_id,
        target_type="crm_smart_list",
        target_id=row.pk,
    )
    return row


def get(membership: Membership, list_id: uuid.UUID) -> SmartList:
    found = visible_lists(membership).filter(pk=list_id).first()
    if found is None:
        raise NotFoundError("smart list not found")
    return found


def update(
    *, membership: Membership, actor: Actor, list_id: uuid.UUID, data: dict[str, Any]
) -> SmartList:
    row = get(membership, list_id)
    if row.owner_id != membership.user_id:
        raise PermissionDeniedError("only the owner can change a smart list")
    name = data.get("name", row.name)
    spec = data.get("filter", row.filter)
    columns = data.get("columns", row.columns)
    row.name = _check(membership.workspace_id, name, spec, columns)
    row.filter, row.columns = spec, columns
    if "visibility" in data:
        if data["visibility"] not in Visibility.values:
            raise ValidationFailedError(
                "invalid visibility", errors={"visibility": "invalid_value"}
            )
        row.visibility = data["visibility"]
    row.save()
    record_event(
        action="crm.smart_list.updated",
        actor=actor,
        workspace_id=membership.workspace_id,
        target_type="crm_smart_list",
        target_id=row.pk,
    )
    return row


def delete(*, membership: Membership, actor: Actor, list_id: uuid.UUID) -> None:
    row = get(membership, list_id)
    if row.owner_id != membership.user_id:
        raise PermissionDeniedError("only the owner can delete a smart list")
    row.delete()
    record_event(
        action="crm.smart_list.deleted",
        actor=actor,
        workspace_id=membership.workspace_id,
        target_type="crm_smart_list",
        target_id=list_id,
    )


def contacts_of(
    membership: Membership, row: SmartList, *, text: str | None = None
) -> QuerySet[Contact]:
    """Evaluate the list now, with the viewer's own record scope."""
    return contacts.query_contacts(membership, spec=row.filter, text=text)
