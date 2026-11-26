"""CSV export of deals: the same filters as the list, the viewer's scope, and a size limit."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

from django.conf import settings

from audit.actors import Actor
from audit.services import record_event
from core.errors import ValidationFailedError
from crm.pipelines import services
from crm.pipelines.models import Opportunity
from workspaces import services as workspaces
from workspaces.models import Membership

MICROS = 1_000_000
COLUMNS = (
    "id",
    "name",
    "contact",
    "pipeline",
    "stage",
    "status",
    "value",
    "currency",
    "owner",
    "priority",
    "tags",
    "source",
    "expected_close_date",
    "created_at",
    "closed_at",
)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime | date) else value


def _row(deal: Opportunity, owners: dict[Any, str]) -> list[object]:
    return [
        deal.pk,
        deal.name,
        str(deal.contact),
        deal.pipeline.name,
        deal.stage.name,
        deal.status,
        deal.value_micros / MICROS,
        deal.currency,
        owners.get(deal.assigned_to_id, ""),
        deal.priority,
        "; ".join(deal.tags),
        deal.source,
        _iso(deal.expected_close_date),
        _iso(deal.created_at),
        _iso(deal.closed_at),
    ]


def rows(
    *, membership: Membership, actor: Actor, sort: str | None, **criteria: Any
) -> Iterator[list[object]]:
    """Header first, then data. Raises before yielding anything if the export is too large."""
    found = services.list_opportunities(membership.workspace_id, membership, sort=sort, **criteria)
    limit = settings.CRM_EXPORT_MAX_ROWS
    deals = list(found.select_related("pipeline")[: limit + 1])
    if len(deals) > limit:
        raise ValidationFailedError("too many rows", errors={"export": "too_many_rows"})
    record_event(
        action="crm.opportunities.exported",
        actor=actor,
        workspace_id=membership.workspace_id,
        target_type="crm_opportunity",
        after={"rows": len(deals)},
    )
    owners = {m.user_id: m.user.email for m in workspaces.list_members(membership.workspace_id)}
    return _generate(deals, owners)


def _generate(deals: list[Opportunity], owners: dict[Any, str]) -> Iterator[list[object]]:
    yield list(COLUMNS)
    for deal in deals:
        yield _row(deal, owners)
