"""Delivery feedback from providers: bounces and spam complaints."""

from __future__ import annotations

import uuid

from audit.actors import Actor
from crm.consent import services as consent
from crm.consent.models import Purpose, Source, Status
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.inbox.models import Direction, Message
from crm.sending import suppression
from crm.sending.models import SuppressionReason
from crm.sending.rules import ADDRESS_IDENTITY

BOUNCED = "bounced"
SYSTEM = Actor.system("delivery-feedback")


def _message(workspace_id: uuid.UUID, channel: str, provider_message_id: str) -> Message | None:
    return (
        Message.objects.for_workspace(workspace_id)
        .select_related("conversation")
        .filter(
            channel=channel,
            provider_message_id=provider_message_id,
            direction=Direction.OUTBOUND,
        )
        .first()
    )


def _address(message: Message) -> str:
    return contacts.identity_value(
        message.workspace_id, message.conversation.contact_id, ADDRESS_IDENTITY[message.channel]
    )


def record_bounce(
    *, workspace_id: uuid.UUID, channel: str, provider_message_id: str, hard: bool
) -> Message | None:
    """A message came back undeliverable. A hard bounce also suppresses the address for good."""
    message = _message(workspace_id, channel, provider_message_id)
    if message is None:
        return None
    inbox.set_failed(
        workspace_id=workspace_id, message_id=message.pk, error_code=BOUNCED, force=True
    )
    if hard:
        address = _address(message)
        if address:
            suppression.suppress(
                workspace_id=workspace_id,
                actor=SYSTEM,
                channel=channel,
                address=address,
                reason=SuppressionReason.HARD_BOUNCE,
            )
    message.refresh_from_db()
    return message


def record_complaint(
    *, workspace_id: uuid.UUID, channel: str, provider_message_id: str
) -> Message | None:
    """The recipient marked the message as spam: suppress the address and stop marketing."""
    message = _message(workspace_id, channel, provider_message_id)
    if message is None:
        return None
    address = _address(message)
    if address:
        suppression.suppress(
            workspace_id=workspace_id,
            actor=SYSTEM,
            channel=channel,
            address=address,
            reason=SuppressionReason.COMPLAINT,
        )
    consent.record(
        workspace_id=workspace_id,
        actor=SYSTEM,
        contact_id=message.conversation.contact_id,
        channel=channel,
        purpose=Purpose.MARKETING,
        status=Status.OPTED_OUT,
        source=Source.API,
        evidence={"complaint": True, "message_id": str(message.pk)},
    )
    return message
