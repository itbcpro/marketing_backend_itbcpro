"""Publish events through the transactional outbox."""

import uuid
from typing import Any

from django.db import connection
from django.utils import timezone
from pydantic import BaseModel

from audit.actors import Actor
from audit.outbox import enqueue
from core.ids import new_id
from crm.events import registry
from crm.events.envelope import EventActor, EventEnvelope


def publish(
    event_type: str,
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    data: BaseModel,
    version: int = 1,
) -> EventEnvelope:
    """Validate the data against its registered model and enqueue the envelope.

    Must run inside the transaction of the change it announces, so a rollback drops the event.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("publish() must be called inside a transaction")
    model = registry.data_model(event_type, version)
    if not isinstance(data, model):
        raise TypeError(f"{event_type} v{version} expects {model.__name__}")
    envelope = EventEnvelope(
        id=new_id(),
        type=event_type,
        version=version,
        tenant_id=workspace_id,
        occurred_at=timezone.now(),
        actor=EventActor(type=actor.type, id=actor.id),
        data=_json(data),
    )
    enqueue(event_type, envelope.model_dump(mode="json"))
    return envelope


def _json(data: BaseModel) -> dict[str, Any]:
    return data.model_dump(mode="json")
