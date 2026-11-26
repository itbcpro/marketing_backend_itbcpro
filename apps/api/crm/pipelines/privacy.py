"""Deals of one person, for an access request."""

from __future__ import annotations

import uuid
from typing import Any

from crm.pipelines.models import Opportunity, Payment


def export_payments(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = Payment.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return [
        {
            "kind": p.kind,
            "status": p.status,
            "method": p.method,
            "amount_micros": p.amount_micros,
            "currency": p.currency,
            "reference": p.reference,
            "at": p.occurred_at,
        }
        for p in rows.order_by("occurred_at")
    ]


def export_contact_data(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = Opportunity.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return [
        {
            "name": d.name,
            "status": d.status,
            "stage": d.stage.name,
            "value_micros": d.value_micros,
            "currency": d.currency,
            "closed_at": d.closed_at,
            "at": d.created_at,
        }
        for d in rows.select_related("stage").order_by("created_at")
    ]
