"""Background jobs of the workflow engine."""

from __future__ import annotations

import uuid
from typing import Any

from core.tasks import reliable_task, tenant_task
from crm.workflows import engine

QUEUE = "default"


@tenant_task(engine.RUN_TASK, queue=QUEUE)
def run_enrollment(self: Any, *, workspace_id: str, enrollment_id: str) -> str:
    """Run one contact's workflow until it has to wait or is done."""
    return engine.run(uuid.UUID(workspace_id), uuid.UUID(enrollment_id))


@reliable_task("crm.workflows.wake", queue=QUEUE)
def wake(self: Any) -> int:
    """Hand every wait that is over (and every run nobody picked up) to a worker."""
    return engine.wake_due()
