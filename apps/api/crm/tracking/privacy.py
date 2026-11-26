"""What the tracking module holds about one person: export, erasure and retention."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from crm.tracking.models import EmailEvent


def export_contact_data(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = EmailEvent.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return [{"kind": e.kind, "url": e.url, "at": e.created_at} for e in rows.order_by("created_at")]


def erase_contact(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> int:
    deleted, _ = (
        EmailEvent.objects.for_workspace(workspace_id).filter(contact_id=contact_id).delete()
    )
    return int(deleted)


def purge_events(cutoff: datetime) -> int:
    deleted, _ = EmailEvent.objects.filter(created_at__lt=cutoff).delete()
    return int(deleted)
