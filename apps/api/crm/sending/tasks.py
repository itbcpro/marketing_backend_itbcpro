"""Delivery job: runs in the tenant of its workspace and retries temporary provider failures."""

import uuid
from typing import Any

from django.conf import settings

from core.tasks import reliable_task, tenant_task
from crm.sending import delivery, domains
from crm.sending.ports import RetryableAdapterError

BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 1800


@tenant_task("crm.sending.deliver", queue="default")
def deliver(self: Any, *, workspace_id: str, message_id: str) -> str:
    attempt = self.request.retries
    final = attempt >= settings.CRM_DELIVER_MAX_RETRIES
    try:
        message = delivery.deliver(
            workspace_id=uuid.UUID(workspace_id),
            message_id=uuid.UUID(message_id),
            final_attempt=final,
        )
    except RetryableAdapterError as error:
        countdown = min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS)
        raise self.retry(
            exc=error, countdown=countdown, max_retries=settings.CRM_DELIVER_MAX_RETRIES
        ) from error
    return str(message.status)


@reliable_task("crm.sending.recheck_domains_all", queue="low")
def recheck_domains_all(self: Any) -> int:
    """Fan out one DNS re-check per sending domain (records can be removed after verification)."""
    pairs = domains.due_for_recheck()
    for workspace_id, domain_id in pairs:
        recheck_domain.delay(workspace_id=str(workspace_id), domain_id=str(domain_id))
    return len(pairs)


@tenant_task("crm.sending.recheck_domain", queue="low")
def recheck_domain(self: Any, *, workspace_id: str, domain_id: str) -> str:
    domain = domains.verify(workspace_id=uuid.UUID(workspace_id), domain_id=uuid.UUID(domain_id))
    return str(domain.status)
