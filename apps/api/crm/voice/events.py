"""Event data models of the voice module."""

import uuid

from pydantic import BaseModel

from crm.events.registry import event


class CallRef(BaseModel):
    call_id: uuid.UUID
    contact_id: uuid.UUID
    direction: str


@event("crm.call.completed")
class CallCompleted(CallRef):
    duration_seconds: int


@event("crm.call.missed")
class CallMissed(CallRef):
    pass


@event("crm.call.voicemail")
class CallVoicemail(CallRef):
    recording_seconds: int
