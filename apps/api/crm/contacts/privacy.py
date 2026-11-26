"""What the contacts module holds about one person, for an access request."""

from __future__ import annotations

import uuid
from typing import Any

from crm.contacts import selectors
from crm.contacts.models import Contact, ContactIdentity


def export_contact_data(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> dict[str, Any]:
    contact = (
        Contact.objects.for_workspace(workspace_id).select_related("company").get(pk=contact_id)
    )
    profile = selectors.serialise(workspace_id, [contact])[0]
    identities = ContactIdentity.objects.for_workspace(workspace_id).filter(contact=contact)
    return {
        "profile": profile,
        "company": contact.company.name if contact.company else "",
        "identities": [
            {"channel": i.channel, "value": i.value, "active": i.is_active} for i in identities
        ],
        "created_at": contact.created_at,
    }
