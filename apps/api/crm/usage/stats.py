"""Read-only usage totals across the workspaces of one organization, for invoices."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db.models import Sum

from crm.usage.models import UsageRecord


def usage_by_channel(
    organization_id: uuid.UUID, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Units and price per channel for everything rated in the period."""
    rows = (
        UsageRecord.objects.filter(
            workspace__organization_id=organization_id,
            rated=True,
            created_at__gte=since,
            created_at__lt=until,
        )
        .values("channel")
        .annotate(units=Sum("units"), price=Sum("price_micros"))
        .order_by("channel")
    )
    return [
        {
            "channel": r["channel"],
            "units": int(r["units"] or 0),
            "price_micros": int(r["price"] or 0),
        }
        for r in rows
    ]
