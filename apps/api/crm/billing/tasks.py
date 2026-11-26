"""The monthly invoice run."""

from __future__ import annotations

from typing import Any

from core.tasks import reliable_task
from crm.billing import services

QUEUE = "low"


@reliable_task("crm.billing.generate_invoices", queue=QUEUE)
def generate_invoices(self: Any) -> int:
    """Invoice the month that just ended. Safe to run every day: each month is made once."""
    return services.generate_all()
