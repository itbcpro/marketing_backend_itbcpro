"""Scheduled audit and outbox jobs."""

from celery import shared_task
from django.conf import settings

from audit import outbox, retention, services
from core.logging import get_logger

log = get_logger(__name__)


@shared_task(name="audit.dispatch_outbox", queue="default", time_limit=120, soft_time_limit=100)
def dispatch_outbox() -> int:
    return outbox.dispatch_batch()


@shared_task(name="audit.run_retention", queue="low", time_limit=900, soft_time_limit=840)
def run_retention() -> dict[str, int]:
    return retention.run_retention(
        audit_days=settings.AUDIT_RETENTION_DAYS, outbox_days=settings.OUTBOX_RETENTION_DAYS
    )


@shared_task(name="audit.verify_chain", queue="low", time_limit=600, soft_time_limit=540)
def verify_chain() -> int:
    """Verify every chain; returns the number of broken chains and logs each one."""
    broken = 0
    for chain in services.list_chains():
        first_bad = services.verify_chain(chain)
        if first_bad is not None:
            broken += 1
            log.error("audit_chain_broken", chain=str(chain), first_bad_seq=first_bad)
    return broken
