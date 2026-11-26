"""Everything the CRM holds about one person, gathered for an access request."""

from __future__ import annotations

import json
import uuid
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from crm.activity import privacy as activity
from crm.consent import privacy as consent
from crm.contacts import privacy as contacts
from crm.inbox import privacy as inbox
from crm.leads import privacy as leads
from crm.pipelines import privacy as pipelines
from crm.tracking import privacy as tracking
from crm.voice import privacy as voice
from crm.workflows import privacy as workflows

FORMAT_VERSION = 1


def export_contact(workspace_id: uuid.UUID, actor: Actor, contact_id: uuid.UUID) -> dict[str, Any]:
    """The person's data as plain JSON. The export itself is written to the audit log."""
    data = {
        "format_version": FORMAT_VERSION,
        "generated_at": timezone.now(),
        "contact": contacts.export_contact_data(workspace_id, contact_id),
        "consent": consent.export_contact_data(workspace_id, contact_id),
        "messages": inbox.export_contact_data(workspace_id, contact_id),
        **activity.export_contact_data(workspace_id, contact_id),
        "calls": voice.export_contact_data(workspace_id, contact_id),
        "email_activity": tracking.export_contact_data(workspace_id, contact_id),
        "deals": pipelines.export_contact_data(workspace_id, contact_id),
        "payments": pipelines.export_payments(workspace_id, contact_id),
        "lead_forms": leads.export_contact_data(workspace_id, contact_id),
        "automations": workflows.export_contact_data(workspace_id, contact_id),
    }
    record_event(
        action="crm.contact.exported",
        actor=actor,
        workspace_id=workspace_id,
        target_type="crm_contact",
        target_id=contact_id,
    )
    plain: dict[str, Any] = json.loads(json.dumps(data, cls=DjangoJSONEncoder))
    return plain
