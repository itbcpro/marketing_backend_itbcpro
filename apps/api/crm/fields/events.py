"""Event data models of the fields module."""

import uuid

from pydantic import BaseModel

from crm.events.registry import event


@event("crm.field.created")
class FieldCreated(BaseModel):
    field_id: uuid.UUID
    object_type: str
    key: str
    field_type: str


@event("crm.field.archived")
class FieldArchived(BaseModel):
    field_id: uuid.UUID
    object_type: str
    key: str
