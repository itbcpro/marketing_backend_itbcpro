"""Turn CRM events into notifications for the people they concern."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from audit.outbox import register_handler
from core.errors import NotFoundError
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope
from crm.inbox import services as inbox
from crm.notifications import services
from crm.notifications.models import NotificationKind

Recipients = list[tuple[uuid.UUID, str, dict[str, Any]]]


def _uuid(value: Any) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None


def _task(envelope: EventEnvelope) -> Recipients:
    to = _uuid(envelope.data.get("assigned_to"))
    data = {"task_id": envelope.data["task_id"], "contact_id": envelope.data["contact_id"]}
    return [(to, NotificationKind.TASK_ASSIGNED, data)] if to else []


def _mentions(envelope: EventEnvelope) -> Recipients:
    data = {"note_id": envelope.data["note_id"], "contact_id": envelope.data["contact_id"]}
    return [(uuid.UUID(str(m)), NotificationKind.MENTION, data) for m in envelope.data["mentions"]]


def _assignment(kind: str, id_key: str) -> Callable[[EventEnvelope], Recipients]:
    def build(envelope: EventEnvelope) -> Recipients:
        to = _uuid(envelope.data.get("assigned_to"))
        data = {id_key: envelope.data[id_key], "contact_id": envelope.data["contact_id"]}
        return [(to, kind, data)] if to else []

    return build


def _inbound(envelope: EventEnvelope) -> Recipients:
    try:
        conversation = inbox.get_conversation(
            envelope.tenant_id, uuid.UUID(str(envelope.data["conversation_id"]))
        )
    except NotFoundError:
        return []
    if conversation.assigned_to_id is None:
        return []
    data = {
        "conversation_id": envelope.data["conversation_id"],
        "contact_id": envelope.data["contact_id"],
    }
    return [(conversation.assigned_to_id, NotificationKind.CUSTOMER_REPLIED, data)]


SOURCES: dict[str, Callable[[EventEnvelope], Recipients]] = {
    "crm.task.created": _task,
    "crm.note.added": _mentions,
    "crm.contact.assigned": _assignment(NotificationKind.CONTACT_ASSIGNED, "contact_id"),
    "crm.opportunity.assigned": _assignment(NotificationKind.DEAL_ASSIGNED, "opportunity_id"),
    "crm.conversation.assigned": _assignment(
        NotificationKind.CONVERSATION_ASSIGNED, "conversation_id"
    ),
    "crm.message.inbound": _inbound,
}


def _handler(topic: str) -> Callable[[dict[str, Any]], None]:
    build = SOURCES[topic]

    @idempotent(f"notifications.{topic}")
    def handle(envelope: EventEnvelope) -> None:
        for recipient, kind, data in build(envelope):
            services.notify(
                envelope.tenant_id,
                recipient=recipient,
                actor_id=envelope.actor.id,
                kind=kind,
                data=data,
            )

    return handle


def register() -> None:
    for topic in SOURCES:
        register_handler(topic, _handler(topic))
