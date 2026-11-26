"""The one envelope every CRM event travels in (contracts/events/envelope.schema.json)."""

import uuid
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class EventActor(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str = Field(min_length=1, max_length=20)
    id: str = Field(min_length=1, max_length=100)


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: uuid.UUID
    type: str = Field(pattern=EVENT_TYPE_PATTERN, max_length=100)
    version: int = Field(ge=1)
    tenant_id: uuid.UUID
    occurred_at: AwareDatetime
    actor: EventActor
    data: dict[str, Any]


def parse(payload: dict[str, Any]) -> EventEnvelope:
    return EventEnvelope.model_validate(payload)
