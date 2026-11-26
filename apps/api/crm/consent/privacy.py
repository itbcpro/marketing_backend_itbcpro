"""The consent history of one person, for an access request."""

from __future__ import annotations

import uuid
from typing import Any

from crm.consent.models import ConsentEvent


def export_contact_data(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = ConsentEvent.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return [
        {
            "channel": r.channel,
            "purpose": r.purpose,
            "status": r.status,
            "source": r.source,
            "evidence": r.evidence,
            "occurred_at": r.occurred_at,
        }
        for r in rows.order_by("occurred_at")
    ]
