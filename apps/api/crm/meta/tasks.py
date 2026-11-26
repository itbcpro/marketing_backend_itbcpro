"""Background jobs of the Meta channels."""

from __future__ import annotations

import uuid
from typing import Any

from core.errors import DomainError
from core.tasks import reliable_task, tenant_task
from crm.meta import templates
from crm.meta.models import MetaAccount

QUEUE = "default"


@reliable_task("crm.meta.sync_templates_all", queue=QUEUE)
def sync_templates_all(self: Any) -> int:
    """Hand each connected WhatsApp account to its own tenant job."""
    ids = list(
        MetaAccount.objects.filter(is_active=True)
        .exclude(waba_id="")
        .values_list("workspace_id", flat=True)
    )
    for workspace_id in ids:
        sync_templates.delay(workspace_id=str(workspace_id))
    return len(ids)


@tenant_task("crm.meta.sync_templates", queue=QUEUE)
def sync_templates(self: Any, *, workspace_id: str) -> int:
    try:
        return templates.sync(uuid.UUID(workspace_id))
    except DomainError:
        return 0  # a revoked token or a paused account is reported on the setup screen
