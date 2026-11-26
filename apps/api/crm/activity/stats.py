"""Read-only aggregates over tasks, for reports."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db.models import Count

from crm.activity.models import Task, TaskStatus


def tasks_completed_by_user(
    workspace_id: uuid.UUID, since: datetime, until: datetime
) -> dict[str, int]:
    rows = (
        Task.objects.for_workspace(workspace_id)
        .filter(status=TaskStatus.COMPLETE, completed_at__gte=since, completed_at__lt=until)
        .exclude(completed_by__isnull=True)
        .values("completed_by_id")
        .annotate(count=Count("id"))
    )
    return {str(r["completed_by_id"]): int(r["count"]) for r in rows}
