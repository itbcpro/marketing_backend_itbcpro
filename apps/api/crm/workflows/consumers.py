"""Events that start workflows, and the events that move people already inside them."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from audit.outbox import register_handler
from core.errors import NotFoundError
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope
from crm.workflows import engine, triggers
from crm.workflows.models import Workflow, WorkflowStatus

INBOUND = "crm.message.inbound"


def _enrol_matching(envelope: EventEnvelope, event_type: str) -> None:
    active = Workflow.objects.for_workspace(envelope.tenant_id).filter(
        status=WorkflowStatus.ACTIVE, archived_at__isnull=True, published_version__isnull=False
    )
    for workflow in active.select_related("published_version"):
        spec = workflow.published_version.definition.get("trigger", {})  # type: ignore[union-attr]
        trigger = triggers.get(str(spec.get("type", "")))
        if trigger is None or trigger.event_type != event_type:
            continue
        if not trigger.matches(envelope.data, spec.get("config") or {}):
            continue
        contact_id = trigger.contact_id(envelope.data)
        if contact_id is None:
            continue
        try:
            engine.enroll(envelope.tenant_id, workflow.pk, contact_id, source=trigger.key)
        except NotFoundError:
            continue  # the contact was erased before the event was handled


def _trigger_handler(event_type: str) -> Callable[[dict[str, Any]], None]:
    @idempotent(f"workflows.trigger.{event_type}")
    def handle(envelope: EventEnvelope) -> None:
        _enrol_matching(envelope, event_type)

    return handle


@idempotent("workflows.inbound")
def _inbound(envelope: EventEnvelope) -> None:
    engine.on_inbound_message(envelope.tenant_id, uuid.UUID(str(envelope.data["contact_id"])))


def register() -> None:
    for event_type in triggers.event_types():
        register_handler(event_type, _trigger_handler(event_type))
    register_handler(INBOUND, _inbound)
