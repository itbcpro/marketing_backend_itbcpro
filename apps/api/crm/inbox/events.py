"""Event data models of the inbox module. They carry ids only, never message bodies."""

import uuid

from pydantic import BaseModel

from crm.events.registry import event


class MessageRef(BaseModel):
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    channel: str


@event("crm.message.inbound")
class MessageInbound(MessageRef):
    pass


@event("crm.message.outbound")
class MessageOutbound(MessageRef):
    pass


@event("crm.message.status_changed")
class MessageStatusChanged(MessageRef):
    status: str


@event("crm.conversation.assigned")
class ConversationAssigned(BaseModel):
    conversation_id: uuid.UUID
    contact_id: uuid.UUID
    assigned_to: uuid.UUID | None
