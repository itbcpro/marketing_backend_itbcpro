"""Background jobs of the campaigns module."""

import uuid
from typing import Any

from core.tasks import reliable_task, tenant_task
from crm.campaigns import services

QUEUE = "default"


@reliable_task("crm.campaigns.tick", queue=QUEUE)
def tick(self: Any) -> int:
    """Look for campaigns that are due and hand each to its own tenant job."""
    due = services.due_campaigns()
    for workspace_id, campaign_id, action in due:
        step.delay(workspace_id=str(workspace_id), campaign_id=str(campaign_id), action=action)
    return len(due)


@tenant_task("crm.campaigns.step", queue=QUEUE)
def step(self: Any, *, workspace_id: str, campaign_id: str, action: str) -> str:
    ws, cid = uuid.UUID(workspace_id), uuid.UUID(campaign_id)
    if action == "start":
        return str(services.start_scheduled(workspace_id=ws, campaign_id=cid).status)
    campaign = services.process_batch(workspace_id=ws, campaign_id=cid)
    return str(campaign.status) if campaign else "skipped"
