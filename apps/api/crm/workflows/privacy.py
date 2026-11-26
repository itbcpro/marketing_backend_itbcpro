"""Automations one person went through, and the retention of finished runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from crm.workflows.models import OPEN_STATUSES, Enrollment


def export_contact_data(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = Enrollment.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return [
        {
            "workflow": e.workflow.name,
            "status": e.status,
            "exit_reason": e.exit_reason,
            "at": e.created_at,
            "finished_at": e.finished_at,
        }
        for e in rows.select_related("workflow").order_by("created_at")
    ]


def purge_finished(cutoff: datetime) -> int:
    """Delete runs that ended before the cutoff, with their step logs."""
    stale = Enrollment.objects.filter(finished_at__lt=cutoff).exclude(status__in=OPEN_STATUSES)
    deleted, _ = stale.delete()
    return int(deleted)
