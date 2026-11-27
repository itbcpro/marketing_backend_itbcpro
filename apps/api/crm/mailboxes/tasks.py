"""Reading replies from connected mailboxes, on a schedule."""

from __future__ import annotations

import uuid
from typing import Any

from core.tasks import reliable_task, tenant_task
from crm.mailboxes import services


@reliable_task("crm.mailboxes.sync_all", queue="low")
def sync_all(self: Any) -> int:
    """Fan out one read per mailbox that has reading switched on, and tidy old sign-in requests."""
    services.purge_expired_requests()
    pairs = services.syncable()
    for workspace_id, mailbox_id in pairs:
        sync_one.delay(workspace_id=str(workspace_id), mailbox_id=str(mailbox_id))
    return len(pairs)


@tenant_task("crm.mailboxes.sync_one", queue="low")
def sync_one(self: Any, *, workspace_id: str, mailbox_id: str) -> int:
    return services.sync(workspace_id=uuid.UUID(workspace_id), mailbox_id=uuid.UUID(mailbox_id))
