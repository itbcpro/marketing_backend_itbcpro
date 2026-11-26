"""Push inbox changes to connected browsers (ids only; clients fetch details through the API)."""

from collections.abc import Callable
from typing import Any

from audit.outbox import register_handler
from core import sse
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope

TOPICS = (
    "crm.message.inbound",
    "crm.message.outbound",
    "crm.message.status_changed",
    "crm.conversation.assigned",
)
STREAM_EVENT = "inbox"


def _pusher(topic: str) -> Callable[[dict[str, Any]], None]:
    @idempotent(f"inbox.push.{topic}")
    def push(envelope: EventEnvelope) -> None:
        data = {k: v for k, v in envelope.data.items() if k.endswith("_id") or k == "status"}
        sse.publish(
            sse.workspace_channel(envelope.tenant_id),
            STREAM_EVENT,
            {"topic": topic, **data},
        )

    return push


def register() -> None:
    for topic in TOPICS:
        register_handler(topic, _pusher(topic))
