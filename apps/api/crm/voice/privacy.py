"""Calls with one person, and what retention does to their recordings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from crm.voice.models import Call


def export_contact_data(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = Call.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return [
        {
            "direction": c.direction,
            "from": c.from_number,
            "to": c.to_number,
            "status": c.status,
            "duration_seconds": c.duration_seconds,
            "voicemail": c.voicemail,
            "recording_url": c.recording_url,
            "transcript": c.transcript,
            "at": c.created_at,
        }
        for c in rows.order_by("created_at")
    ]


def purge_recordings(cutoff: datetime) -> int:
    """Forget the recording link and the transcript of old calls; the call itself stays."""
    stale = Call.objects.filter(created_at__lt=cutoff).exclude(recording_url="", transcript="")
    return int(stale.update(recording_url="", transcript=""))
