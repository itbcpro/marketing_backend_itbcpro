"""Event data models of the consent module."""

import uuid

from pydantic import BaseModel

from crm.events.registry import event


@event("crm.consent.changed")
class ConsentChanged(BaseModel):
    contact_id: uuid.UUID
    channel: str
    purpose: str
    status: str
    source: str
