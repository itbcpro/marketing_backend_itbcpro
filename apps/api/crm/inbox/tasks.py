"""Background jobs of the inbox module."""

import uuid
from typing import Any

from core.tasks import reliable_task, tenant_task
from crm.inbox import instagram_bridge

QUEUE = "ingest.social"


@reliable_task("crm.inbox.bridge_instagram_all", queue=QUEUE)
def bridge_instagram_all(self: Any) -> int:
    """Fan out one bridge run per workspace that has an Instagram account."""
    workspaces = instagram_bridge.workspaces_with_instagram()
    for workspace_id in workspaces:
        bridge_instagram.delay(workspace_id=str(workspace_id))
    return len(workspaces)


@tenant_task("crm.inbox.bridge_instagram", queue=QUEUE)
def bridge_instagram(self: Any, *, workspace_id: str) -> dict[str, int]:
    return instagram_bridge.run(uuid.UUID(workspace_id))
