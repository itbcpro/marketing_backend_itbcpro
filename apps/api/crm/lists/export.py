"""CSV export of contacts with the viewer's scope and a size limit."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

from django.conf import settings

from audit.actors import Actor
from audit.services import record_event
from core.errors import ValidationFailedError
from crm.contacts import services as contacts
from workspaces.models import Membership

CHUNK = 500
STANDARD_COLUMNS = (
    "id",
    "first_name",
    "last_name",
    "email",
    "phone",
    "contact_type",
    "source",
    "city",
    "state",
    "postal_code",
    "country",
    "assigned_to",
    "created_at",
    "tags",
)
CUSTOM_PREFIX = "custom."


def _cell(item: dict[str, Any], column: str) -> object:
    if column == "tags":
        return "; ".join(item["tags"])
    if column.startswith(CUSTOM_PREFIX):
        return item["custom"].get(column.removeprefix(CUSTOM_PREFIX), "")
    value: Any = item.get(column)
    return value.isoformat() if isinstance(value, datetime | date) else value


def rows(
    *,
    membership: Membership,
    actor: Actor,
    spec: dict[str, Any] | None,
    text: str | None,
    columns: list[str] | None,
) -> Iterator[list[object]]:
    """Header first, then data. Raises before yielding anything if the export is too large."""
    chosen = columns or list(STANDARD_COLUMNS)
    bad = [c for c in chosen if c not in STANDARD_COLUMNS and not c.startswith(CUSTOM_PREFIX)]
    if bad:
        raise ValidationFailedError("invalid columns", errors={"columns": "invalid_value"})
    query = contacts.query_contacts(membership, spec=spec, text=text).order_by("pk")
    limit = settings.CRM_EXPORT_MAX_ROWS
    ids = list(query.values_list("pk", flat=True)[: limit + 1])
    if len(ids) > limit:
        raise ValidationFailedError("too many rows", errors={"export": "too_many_rows"})
    record_event(
        action="crm.contacts.exported",
        actor=actor,
        workspace_id=membership.workspace_id,
        target_type="crm_contact",
        after={"rows": len(ids), "columns": chosen},
    )
    return _generate(membership, chosen, ids)


def _generate(membership: Membership, columns: list[str], ids: list[Any]) -> Iterator[list[object]]:
    yield list(columns)
    for start in range(0, len(ids), CHUNK):
        batch = list(
            contacts.query_contacts(membership)
            .filter(pk__in=ids[start : start + CHUNK])
            .order_by("pk")
        )
        for item in contacts.serialise_contacts(membership.workspace_id, batch):
            yield [_cell(item, column) for column in columns]
