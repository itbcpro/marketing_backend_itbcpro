"""Retention of notifications people have already seen."""

from __future__ import annotations

from datetime import datetime

from crm.notifications.models import Notification


def purge_read(cutoff: datetime) -> int:
    deleted, _ = Notification.objects.filter(read_at__isnull=False, created_at__lt=cutoff).delete()
    return int(deleted)
