"""Read-only aggregates over messages, for reports."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db.models import Count

from crm.inbox.models import Direction, Message, MessageStatus


def get_message(workspace_id: uuid.UUID, message_id: uuid.UUID) -> Message | None:
    return Message.objects.for_workspace(workspace_id).filter(pk=message_id).first()


def outbound_count_since(workspace_id: uuid.UUID, since: datetime) -> int:
    """Messages we actually tried to send (blocked ones never count against a plan)."""
    return (
        Message.objects.for_workspace(workspace_id)
        .filter(direction=Direction.OUTBOUND, created_at__gte=since)
        .exclude(status=MessageStatus.BLOCKED)
        .count()
    )


def delivery_stats(
    workspace_id: uuid.UUID, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Outbound messages by channel, status and error code."""
    rows = (
        Message.objects.for_workspace(workspace_id)
        .filter(direction=Direction.OUTBOUND, created_at__gte=since, created_at__lt=until)
        .values("channel", "status", "error_code")
        .annotate(count=Count("id"))
        .order_by("channel", "status", "error_code")
    )
    return [dict(r) for r in rows]


def messages_by_sender(workspace_id: uuid.UUID, since: datetime, until: datetime) -> dict[str, int]:
    """Messages actually sent by each person (blocked and failed ones do not count)."""
    rows = (
        Message.objects.for_workspace(workspace_id)
        .filter(
            direction=Direction.OUTBOUND,
            sent_by_type="human",
            created_at__gte=since,
            created_at__lt=until,
        )
        .exclude(status__in=[MessageStatus.BLOCKED, MessageStatus.FAILED])
        .values("sent_by_id")
        .annotate(count=Count("id"))
    )
    return {str(r["sent_by_id"]): int(r["count"]) for r in rows}
