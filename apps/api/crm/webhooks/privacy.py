"""Retention of the raw webhook archive. Inbound email bodies in it are personal data."""

from __future__ import annotations

from datetime import datetime

from crm.webhooks.models import WebhookDelivery


def purge_deliveries(cutoff: datetime) -> int:
    deleted, _ = WebhookDelivery.objects.filter(created_at__lt=cutoff).delete()
    return int(deleted)
