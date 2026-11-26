"""Report CRM milestones to Meta's Conversions API when a rule asks for it."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from audit.outbox import register_handler
from core.errors import NotFoundError
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope
from crm.leads import capi
from crm.leads.models import ConversionTrigger
from crm.pipelines import services as pipelines

WON = "won"


def _lead_captured(envelope: EventEnvelope) -> None:
    capi.report(
        envelope.tenant_id,
        trigger=ConversionTrigger.LEAD_CAPTURED,
        source_event_id=str(envelope.id),
        contact_id=uuid.UUID(str(envelope.data["contact_id"])),
        leadgen_id=str(envelope.data["leadgen_id"]),
    )


def _deal(envelope: EventEnvelope, trigger: str, stage_id: uuid.UUID | None = None) -> None:
    try:
        deal = pipelines.get_opportunity(
            envelope.tenant_id, uuid.UUID(str(envelope.data["opportunity_id"]))
        )
    except NotFoundError:
        return
    capi.report(
        envelope.tenant_id,
        trigger=trigger,
        source_event_id=str(envelope.id),
        contact_id=deal.contact_id,
        stage_id=stage_id,
        value_micros=deal.value_micros,
        currency=deal.currency,
    )


def _deal_created(envelope: EventEnvelope) -> None:
    _deal(envelope, ConversionTrigger.DEAL_CREATED)


def _deal_status(envelope: EventEnvelope) -> None:
    if envelope.data.get("to_status") == WON:
        _deal(envelope, ConversionTrigger.DEAL_WON)


def _deal_stage(envelope: EventEnvelope) -> None:
    _deal(envelope, ConversionTrigger.STAGE_REACHED, uuid.UUID(str(envelope.data["to_stage_id"])))


SOURCES: dict[str, Callable[[EventEnvelope], None]] = {
    "crm.lead.captured": _lead_captured,
    "crm.opportunity.created": _deal_created,
    "crm.opportunity.status_changed": _deal_status,
    "crm.opportunity.stage_changed": _deal_stage,
}


def _handler(topic: str) -> Callable[[dict[str, Any]], None]:
    run = SOURCES[topic]

    @idempotent(f"leads.capi.{topic}")
    def handle(envelope: EventEnvelope) -> None:
        run(envelope)

    return handle


def register() -> None:
    for topic in SOURCES:
        register_handler(topic, _handler(topic))
