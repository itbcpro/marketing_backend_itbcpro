"""The conversation with one person, for an access request. Internal comments are not included."""

from __future__ import annotations

import uuid
from typing import Any

from crm.inbox.models import Message


def export_contact_data(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = Message.objects.for_workspace(workspace_id).filter(conversation__contact_id=contact_id)
    return [
        {
            "direction": m.direction,
            "channel": m.channel,
            "subject": m.subject,
            "body": m.body,
            "status": m.status,
            "at": m.provider_ts,
        }
        for m in rows.order_by("provider_ts", "id")
    ]
