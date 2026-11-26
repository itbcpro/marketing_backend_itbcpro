"""Retention of the marks that make event handling idempotent."""

from __future__ import annotations

from datetime import datetime

from crm.events.models import ProcessedEvent


def purge_processed(cutoff: datetime) -> int:
    """Marks older than any redelivery can be are no longer needed."""
    deleted, _ = ProcessedEvent.objects.filter(created_at__lt=cutoff).delete()
    return int(deleted)
