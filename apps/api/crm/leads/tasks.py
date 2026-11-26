"""Background jobs of lead capture."""

from __future__ import annotations

import uuid
from typing import Any

from core.errors import RetryableProviderError
from core.tasks import tenant_task
from crm.leads import services
from crm.meta import services as meta

QUEUE = "default"


@tenant_task("crm.leads.capture", queue=QUEUE)
def capture_lead(
    self: Any,
    *,
    workspace_id: str,
    leadgen_id: str,
    form_id: str = "",
    page_id: str = "",
    ad_id: str = "",
) -> str:
    """Fetch one lead from Meta and store it. Temporary trouble is retried with backoff."""
    try:
        row = services.capture(
            uuid.UUID(workspace_id),
            leadgen_id=leadgen_id,
            form_id=form_id,
            page_id=page_id,
            ad_id=ad_id,
        )
    except meta.GraphError as exc:
        if exc.retryable:
            raise RetryableProviderError(exc.code, provider="meta") from exc
        raise
    return "stored" if row else "duplicate"
