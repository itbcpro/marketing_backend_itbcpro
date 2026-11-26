"""Read-only aggregates over contacts, for reports."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from crm.contacts.models import Contact

ROW_LIMIT = 10_000


def count_active(workspace_id: uuid.UUID) -> int:
    return Contact.objects.for_workspace(workspace_id).active().count()


def new_contacts_by_source(
    workspace_id: uuid.UUID, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Contacts created in the period with where they came from (source and campaign)."""
    rows = (
        Contact.objects.for_workspace(workspace_id)
        .active()
        .filter(created_at__gte=since, created_at__lt=until)
        .values("id", "source", "attribution")[:ROW_LIMIT]
    )
    return [
        {
            "id": r["id"],
            "source": r["source"] or "",
            "campaign_id": str((r["attribution"] or {}).get("campaign_id", "")),
        }
        for r in rows
    ]
