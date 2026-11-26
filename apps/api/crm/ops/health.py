"""Is the CRM keeping up? The same signals as the alert rules, readable by a person."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Min

from audit.models import OutboxMessage
from core.models import DeadLetter
from crm.inbox import services as inbox
from crm.workflows.models import OPEN_STATUSES, Enrollment, EnrollmentStatus

ATTEMPTED = ("sent", "delivered", "read", "failed")
HOUR = timedelta(hours=1)
CLOCK_SLACK = timedelta(seconds=5)  # messages written while this runs still count
OK, WARN = "ok", "warn"


@dataclass(frozen=True)
class Totals:
    outbox_lag_seconds: int
    overdue_runs: int
    dead_letters: int


def _outbox_lag(now: datetime) -> int:
    oldest = OutboxMessage.objects.filter(
        processed_at__isnull=True, available_at__lte=now
    ).aggregate(oldest=Min("available_at"))["oldest"]
    return max(0, int((now - oldest).total_seconds())) if oldest else 0


def _overdue(now: datetime, workspace_id: uuid.UUID | None) -> int:
    late = now - timedelta(seconds=settings.CRM_HEALTH_OVERDUE_AFTER_SECONDS)
    rows = Enrollment.objects.filter(status__in=OPEN_STATUSES, wake_at__lte=late)
    rows = rows | Enrollment.objects.filter(status=EnrollmentStatus.ACTIVE, updated_at__lte=late)
    if workspace_id:
        rows = rows.filter(workspace_id=workspace_id)
    return rows.distinct().count()


def _dead_letters(workspace_id: uuid.UUID | None) -> int:
    rows = DeadLetter.objects.filter(resolved_at__isnull=True)
    if workspace_id:
        rows = rows.filter(kwargs__workspace_id=str(workspace_id))
    return rows.count()


def platform_totals(now: datetime) -> Totals:
    return Totals(_outbox_lag(now), _overdue(now, None), _dead_letters(None))


def failure_rate(workspace_id: uuid.UUID, now: datetime) -> tuple[int, float | None]:
    """(messages attempted in the last hour, share of them that failed)."""
    rows = inbox.delivery_stats(workspace_id, now - HOUR, now + CLOCK_SLACK)
    attempted = sum(int(r["count"]) for r in rows if r["status"] in ATTEMPTED)
    failed = sum(int(r["count"]) for r in rows if r["status"] == "failed")
    return attempted, (failed / attempted if attempted else None)


def snapshot(workspace_id: uuid.UUID, now: datetime) -> list[dict[str, Any]]:
    """One entry per signal: its value, its limit and whether it needs attention."""
    lag = _outbox_lag(now)
    overdue = _overdue(now, workspace_id)
    dead = _dead_letters(workspace_id)
    attempted, rate = failure_rate(workspace_id, now)
    noisy = attempted >= settings.CRM_HEALTH_MIN_MESSAGES
    return [
        {
            "key": "outbox_lag",
            "value": lag,
            "limit": settings.CRM_HEALTH_OUTBOX_LAG_SECONDS,
            "status": WARN if lag > settings.CRM_HEALTH_OUTBOX_LAG_SECONDS else OK,
        },
        {
            "key": "overdue_runs",
            "value": overdue,
            "limit": settings.CRM_HEALTH_OVERDUE_RUNS,
            "status": WARN if overdue > settings.CRM_HEALTH_OVERDUE_RUNS else OK,
        },
        {
            "key": "dead_letters",
            "value": dead,
            "limit": 0,
            "status": WARN if dead > 0 else OK,
        },
        {
            "key": "send_failure_percent",
            "value": round((rate or 0) * 100, 1),
            "limit": round(settings.CRM_HEALTH_FAILURE_RATE * 100, 1),
            "status": WARN if noisy and (rate or 0) > settings.CRM_HEALTH_FAILURE_RATE else OK,
        },
    ]
