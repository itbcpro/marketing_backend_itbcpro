"""Notes and tasks about one person, for an access request."""

from __future__ import annotations

import uuid
from typing import Any

from crm.activity.models import Appointment, Note, Task


def export_contact_data(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> dict[str, Any]:
    notes = Note.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    tasks = Task.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    appointments = Appointment.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return {
        "notes": [{"body": n.body, "at": n.created_at} for n in notes.order_by("created_at")],
        "tasks": [
            {"title": t.title, "status": t.status, "due_at": t.due_at, "at": t.created_at}
            for t in tasks.order_by("created_at")
        ],
        "appointments": [
            {"title": a.title, "starts_at": a.starts_at, "status": a.status, "at": a.created_at}
            for a in appointments.order_by("starts_at")
        ],
    }
