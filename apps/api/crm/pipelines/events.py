"""Event data models of the pipelines module."""

import uuid

from pydantic import BaseModel

from crm.events.registry import event


@event("crm.opportunity.created")
class OpportunityCreated(BaseModel):
    opportunity_id: uuid.UUID
    contact_id: uuid.UUID
    pipeline_id: uuid.UUID
    stage_id: uuid.UUID
    value_micros: int
    currency: str


@event("crm.opportunity.stage_changed")
class OpportunityStageChanged(BaseModel):
    opportunity_id: uuid.UUID
    contact_id: uuid.UUID
    from_stage_id: uuid.UUID | None
    to_stage_id: uuid.UUID


@event("crm.opportunity.status_changed")
class OpportunityStatusChanged(BaseModel):
    opportunity_id: uuid.UUID
    contact_id: uuid.UUID
    from_status: str
    to_status: str
    lost_reason_id: uuid.UUID | None


@event("crm.opportunity.assigned")
class OpportunityAssigned(BaseModel):
    opportunity_id: uuid.UUID
    contact_id: uuid.UUID
    assigned_to: uuid.UUID | None


@event("crm.payment.recorded")
class PaymentRecorded(BaseModel):
    payment_id: uuid.UUID
    opportunity_id: uuid.UUID
    contact_id: uuid.UUID
    kind: str
    status: str
    amount_micros: int
    currency: str
