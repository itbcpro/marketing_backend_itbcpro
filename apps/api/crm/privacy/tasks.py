"""Scheduled privacy work."""

from __future__ import annotations

from typing import Any

from core.tasks import reliable_task
from crm.privacy import retention

QUEUE = "low"


@reliable_task("crm.privacy.run_retention", queue=QUEUE)
def run_retention(self: Any) -> dict[str, int]:
    return retention.run()
