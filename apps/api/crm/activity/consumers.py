"""Turn events of the CRM into timeline entries of the contact they concern."""

from collections.abc import Callable
from typing import Any

from audit.outbox import register_handler
from core.errors import NotFoundError
from crm.activity.models import Activity
from crm.contacts import services as contacts
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope

# event type -> (timeline kind, key of the contact id inside the event data)
TIMELINE: dict[str, tuple[str, str]] = {
    "crm.contact.created": ("contact.created", "contact_id"),
    "crm.contact.updated": ("contact.updated", "contact_id"),
    "crm.contact.assigned": ("contact.assigned", "contact_id"),
    "crm.contact.deleted": ("contact.deleted", "contact_id"),
    "crm.contact.restored": ("contact.restored", "contact_id"),
    "crm.contact.merged": ("contact.merged", "winner_id"),
    "crm.contact.merge_undone": ("contact.merge_undone", "winner_id"),
    "crm.tag.added": ("tag.added", "contact_id"),
    "crm.tag.removed": ("tag.removed", "contact_id"),
    "crm.consent.changed": ("consent.changed", "contact_id"),
    "crm.note.added": ("note.added", "contact_id"),
    "crm.task.created": ("task.created", "contact_id"),
    "crm.task.completed": ("task.completed", "contact_id"),
    "crm.appointment.booked": ("appointment.booked", "contact_id"),
    "crm.appointment.status_changed": ("appointment.status_changed", "contact_id"),
    "crm.payment.recorded": ("payment.recorded", "contact_id"),
    "crm.lead.captured": ("lead.captured", "contact_id"),
    "crm.call.completed": ("call.completed", "contact_id"),
    "crm.call.missed": ("call.missed", "contact_id"),
    "crm.call.voicemail": ("call.voicemail", "contact_id"),
    "crm.opportunity.created": ("opportunity.created", "contact_id"),
    "crm.opportunity.stage_changed": ("opportunity.stage_changed", "contact_id"),
    "crm.opportunity.status_changed": ("opportunity.status_changed", "contact_id"),
    "crm.opportunity.assigned": ("opportunity.assigned", "contact_id"),
}


def _writer(event_type: str, kind: str, contact_key: str) -> Callable[[dict[str, Any]], None]:
    @idempotent(f"activity.{event_type}")
    def write(envelope: EventEnvelope) -> None:
        contact_id = envelope.data[contact_key]
        try:
            contact = contacts.get_contact(envelope.tenant_id, contact_id, include_deleted=True)
        except NotFoundError:
            return  # the contact was erased before its history was written
        Activity.objects.create(
            workspace_id=envelope.tenant_id,
            contact=contact,
            kind=kind,
            data={k: v for k, v in envelope.data.items() if k != contact_key},
            actor_type=envelope.actor.type,
            actor_id=envelope.actor.id,
            occurred_at=envelope.occurred_at,
            event_id=envelope.id,
        )

    return write


def register() -> None:
    for event_type, (kind, key) in TIMELINE.items():
        register_handler(event_type, _writer(event_type, kind, key))
