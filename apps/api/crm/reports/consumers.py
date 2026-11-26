"""Keep the analytics tables up to date from the CRM's events."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from django.db import IntegrityError, transaction

from audit.outbox import register_handler
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope
from crm.inbox import services as inbox
from crm.inbox.models import MessageStatus
from crm.reports.models import FirstResponse


@idempotent("reports.first_response.open")
def _opened(envelope: EventEnvelope) -> None:
    conversation_id = uuid.UUID(str(envelope.data["conversation_id"]))
    try:
        with transaction.atomic():
            FirstResponse.objects.create(
                workspace_id=envelope.tenant_id,
                conversation_id=conversation_id,
                contact_id=uuid.UUID(str(envelope.data["contact_id"])),
                channel=str(envelope.data["channel"]),
                inbound_at=envelope.occurred_at,
            )
    except IntegrityError:
        return  # already waiting for an answer: the first message is the one that counts


@idempotent("reports.first_response.close")
def _closed(envelope: EventEnvelope) -> None:
    message = inbox.get_message(envelope.tenant_id, uuid.UUID(str(envelope.data["message_id"])))
    if message is None or message.status in (MessageStatus.BLOCKED, MessageStatus.FAILED):
        return
    open_row = (
        FirstResponse.objects.for_workspace(envelope.tenant_id)
        .select_for_update()
        .filter(
            conversation_id=uuid.UUID(str(envelope.data["conversation_id"])),
            responded_at__isnull=True,
            inbound_at__lte=envelope.occurred_at,
        )
        .first()
    )
    if open_row is None:
        return
    open_row.responded_at = envelope.occurred_at
    open_row.seconds = max(0, int((envelope.occurred_at - open_row.inbound_at).total_seconds()))
    open_row.responder_type = message.sent_by_type
    open_row.responder_id = message.sent_by_id
    open_row.save()


HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "crm.message.inbound": _opened,
    "crm.message.outbound": _closed,
}


def register() -> None:
    for topic, handler in HANDLERS.items():
        register_handler(topic, handler)
