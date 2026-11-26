"""After an erasure, make the id-only tables forget the person too."""

from __future__ import annotations

import uuid

from audit.outbox import register_handler
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope
from crm.leads import privacy as leads
from crm.reports import privacy as reports
from crm.tracking import privacy as tracking

ERASED = "crm.contact.erased"


@idempotent("privacy.erased")
def _erased(envelope: EventEnvelope) -> None:
    contact_id = uuid.UUID(str(envelope.data["contact_id"]))
    reports.anonymise_contact(envelope.tenant_id, contact_id)
    leads.anonymise_conversion_log(envelope.tenant_id, contact_id)
    tracking.erase_contact(envelope.tenant_id, contact_id)


def register() -> None:
    register_handler(ERASED, _erased)
