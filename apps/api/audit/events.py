"""Domain event bus on top of the transactional outbox.

Publishing writes an outbox row in the caller's transaction, so an event exists if and only if the
state change that caused it committed. Subscribers are idempotent handlers registered by topic.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from audit import outbox


class EventTopic(StrEnum):
    """Catalogue of domain events. Add a member before publishing a new kind of event."""

    WORKSPACE_CREATED = "workspace.created"
    MEMBERSHIP_CHANGED = "membership.changed"
    CONNECTION_STATUS_CHANGED = "connection.status_changed"
    ACTION_PROPOSED = "action.proposed"
    ACTION_DECIDED = "action.decided"
    ACTION_EXECUTED = "action.executed"
    METRICS_INGESTED = "metrics.ingested"
    PLATFORM_DRIFT = "platform.drift"


def publish(topic: EventTopic, payload: dict[str, Any]) -> None:
    outbox.enqueue(str(topic), payload)


def subscribe(topic: EventTopic, handler: Callable[[dict[str, Any]], None]) -> None:
    outbox.register_handler(str(topic), handler)
