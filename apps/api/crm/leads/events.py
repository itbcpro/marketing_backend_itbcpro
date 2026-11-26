"""Event data models of the leads module."""

import uuid

from pydantic import BaseModel

from crm.events.registry import event


@event("crm.lead.captured")
class LeadCaptured(BaseModel):
    lead_capture_id: uuid.UUID
    contact_id: uuid.UUID
    leadgen_id: str
    form_id: str
    ad_id: str
    campaign_id: str
    new_contact: bool
