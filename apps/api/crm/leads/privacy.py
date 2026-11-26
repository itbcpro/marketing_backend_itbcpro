"""Lead ad submissions of one person, and the erasure follow-up for the conversion log."""

from __future__ import annotations

import uuid
from typing import Any

from crm.leads.models import ConversionLog, LeadCapture

NIL = uuid.UUID(int=0)


def export_contact_data(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = LeadCapture.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return [
        {
            "form_id": r.form_id,
            "ad_id": r.ad_id,
            "campaign_id": r.campaign_id,
            "answers": r.answers,
            "at": r.created_at,
        }
        for r in rows.order_by("created_at")
    ]


def anonymise_conversion_log(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> int:
    """After an erasure the log keeps what was reported, without saying about whom."""
    rows = ConversionLog.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return int(rows.update(contact_id=NIL))
