"""Retention: how long each kind of data is kept. Periods are settings, rules are data."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from core.logging import get_logger
from crm.events import privacy as events
from crm.notifications import privacy as notifications
from crm.tracking import privacy as tracking
from crm.voice import privacy as voice
from crm.webhooks import privacy as webhooks
from crm.workflows import privacy as workflows

log = get_logger(__name__)


@dataclass(frozen=True)
class Policy:
    key: str
    days_setting: str  # name of the setting holding the period in days
    purge: Callable[[datetime], int]


POLICIES: tuple[Policy, ...] = (
    Policy("webhook_bodies", "CRM_WEBHOOK_RETENTION_DAYS", webhooks.purge_deliveries),
    Policy("processed_events", "CRM_PROCESSED_EVENT_RETENTION_DAYS", events.purge_processed),
    Policy("workflow_runs", "CRM_WORKFLOW_RETENTION_DAYS", workflows.purge_finished),
    Policy("read_notifications", "CRM_NOTIFICATION_RETENTION_DAYS", notifications.purge_read),
    Policy("call_recordings", "CRM_RECORDING_RETENTION_DAYS", voice.purge_recordings),
    Policy("email_activity", "CRM_TRACKING_RETENTION_DAYS", tracking.purge_events),
)


def run(now: datetime | None = None) -> dict[str, int]:
    """Apply every policy once. Returns how many records each one removed or cleared."""
    moment = now or timezone.now()
    out: dict[str, int] = {}
    for policy in POLICIES:
        days = int(getattr(settings, policy.days_setting))
        out[policy.key] = policy.purge(moment - timedelta(days=days))
    log.info("crm_retention_ran", **out)
    return out
