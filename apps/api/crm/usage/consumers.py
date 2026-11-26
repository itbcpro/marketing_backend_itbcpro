"""Charge a message when it has been handed to its provider."""

import uuid

from audit.outbox import register_handler
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope
from crm.inbox.models import MessageStatus
from crm.usage import charging


@idempotent("usage.charge_on_sent")
def charge_on_sent(envelope: EventEnvelope) -> None:
    if envelope.data.get("status") != MessageStatus.SENT:
        return
    charging.charge_message(
        workspace_id=envelope.tenant_id, message_id=uuid.UUID(envelope.data["message_id"])
    )


def register() -> None:
    register_handler("crm.message.status_changed", charge_on_sent)
