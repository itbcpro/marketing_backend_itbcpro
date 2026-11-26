"""Conversation use cases: threads, internal comments, assignment, read state, outbound records."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import NotFoundError, ValidationFailedError
from crm.contacts import services as contacts
from crm.contacts.models import Contact
from crm.events.publisher import publish
from crm.inbox import events
from crm.inbox.models import (
    Conversation,
    ConversationRead,
    ConversationStatus,
    Direction,
    Message,
    MessageStatus,
    MessageStatusEvent,
)
from workspaces.models import Membership

PREVIEW_LENGTH = 140
COMMENT_LIMIT = 5000
# A status may only move forward; failed and blocked can only replace a message still in flight.
RANK = {
    MessageStatus.PENDING: 0,
    MessageStatus.RECEIVED: 0,
    MessageStatus.SENT: 1,
    MessageStatus.DELIVERED: 2,
    MessageStatus.READ: 3,
}
IN_FLIGHT = {MessageStatus.PENDING, MessageStatus.SENT}


def _preview(body: str, subject: str = "") -> str:
    return (body or subject).strip().replace("\n", " ")[:PREVIEW_LENGTH]


def get_or_create(workspace_id: uuid.UUID, contact: Contact) -> Conversation:
    existing = Conversation.objects.for_workspace(workspace_id).filter(contact=contact).first()
    if existing:
        return existing
    try:
        with transaction.atomic():
            return Conversation.objects.create(
                workspace_id=workspace_id, contact=contact, assigned_to_id=contact.assigned_to_id
            )
    except IntegrityError:  # another request created it first
        return Conversation.objects.for_workspace(workspace_id).get(contact=contact)


def get_conversation(workspace_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation:
    found = (
        Conversation.objects.for_workspace(workspace_id)
        .select_related("contact")
        .filter(pk=conversation_id)
        .first()
    )
    if found is None:
        raise NotFoundError("conversation not found")
    return found


def touch(conversation: Conversation, message: Message) -> None:
    """Update the thread summary after a real (non-internal) message."""
    newest = (
        conversation.last_message_at is None or message.provider_ts >= conversation.last_message_at
    )
    if newest:  # a back-filled older message must not rewrite the summary
        conversation.last_message_at = message.provider_ts
        conversation.last_channel = message.channel
        conversation.last_preview = _preview(message.body, message.subject)
    if message.direction == Direction.INBOUND:
        if (
            conversation.last_inbound_at is None
            or message.provider_ts > conversation.last_inbound_at
        ):
            conversation.last_inbound_at = message.provider_ts
        if newest:
            conversation.status = ConversationStatus.OPEN
    conversation.save()


# ---- outbound records (written by the send gateway) ---------------------------------------


def create_outbound(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    contact: Contact,
    channel: str,
    body: str,
    subject: str = "",
    attachments: list[dict[str, Any]] | None = None,
    channel_account_id: uuid.UUID | None = None,
    status: str = MessageStatus.PENDING,
    error_code: str = "",
    idempotency_key: str = "",
    options: dict[str, Any] | None = None,
) -> Message:
    """Store an outbound message (pending, or blocked with a reason) and update the thread."""
    with transaction.atomic():
        conversation = get_or_create(workspace_id, contact)
        message = Message.objects.create(
            workspace_id=workspace_id,
            conversation=conversation,
            channel=channel,
            direction=Direction.OUTBOUND,
            channel_account_id=channel_account_id,
            subject=subject,
            body=body,
            attachments=attachments or [],
            options=options or {},
            status=status,
            error_code=error_code,
            idempotency_key=idempotency_key,
            sent_by_type=actor.type,
            sent_by_id=actor.id,
            provider_ts=timezone.now(),
        )
        touch(conversation, message)
        publish(
            "crm.message.outbound",
            workspace_id=workspace_id,
            actor=actor,
            data=events.MessageOutbound(
                message_id=message.pk,
                conversation_id=conversation.pk,
                contact_id=contact.pk,
                channel=channel,
            ),
        )
    return message


def apply_status(
    *,
    workspace_id: uuid.UUID,
    channel: str,
    provider_message_id: str,
    status: str,
    provider_ts: datetime,
    error_code: str = "",
) -> Message | None:
    """Apply a provider status callback. Repeated or out-of-order callbacks never go backwards."""
    if status not in MessageStatus.values:
        raise ValidationFailedError("invalid status", errors={"status": "invalid_value"})
    with transaction.atomic():
        message = (
            Message.objects.select_for_update()
            .filter(
                workspace_id=workspace_id, channel=channel, provider_message_id=provider_message_id
            )
            .first()
        )
        if message is None:
            return None
        MessageStatusEvent.objects.create(
            workspace_id=workspace_id,
            message=message,
            status=status,
            provider_ts=provider_ts,
            error_code=error_code,
        )
        if not _advances(message.status, status):
            return message
        message.status = status
        if error_code:
            message.error_code = error_code
        message.save(update_fields=["status", "error_code", "updated_at"])
        publish(
            "crm.message.status_changed",
            workspace_id=workspace_id,
            actor=Actor.system("provider"),
            data=events.MessageStatusChanged(
                message_id=message.pk,
                conversation_id=message.conversation_id,
                contact_id=message.conversation.contact_id,
                channel=channel,
                status=status,
            ),
        )
    return message


def _advances(current: str, new: str) -> bool:
    if new in {MessageStatus.FAILED, MessageStatus.BLOCKED}:
        return current in IN_FLIGHT
    if current in {MessageStatus.FAILED, MessageStatus.BLOCKED}:
        return False
    return RANK[MessageStatus(new)] > RANK[MessageStatus(current)]


def set_failed(
    *, workspace_id: uuid.UUID, message_id: uuid.UUID, error_code: str, force: bool = False
) -> Message:
    """Mark a message that never reached the provider as failed, with the reason."""
    with transaction.atomic():
        message = (
            Message.objects.select_for_update()
            .select_related("conversation")
            .filter(workspace_id=workspace_id, pk=message_id)
            .first()
        )
        if message is None:
            raise NotFoundError("message not found")
        if not force and not _advances(message.status, MessageStatus.FAILED):
            return message  # a bounce may arrive after delivery, so it forces the state
        message.status, message.error_code = MessageStatus.FAILED, error_code
        message.save(update_fields=["status", "error_code", "updated_at"])
        publish(
            "crm.message.status_changed",
            workspace_id=workspace_id,
            actor=Actor.system("delivery"),
            data=events.MessageStatusChanged(
                message_id=message.pk,
                conversation_id=message.conversation_id,
                contact_id=message.conversation.contact_id,
                channel=message.channel,
                status=MessageStatus.FAILED,
            ),
        )
    return message


def mark_sent(
    *, workspace_id: uuid.UUID, message_id: uuid.UUID, channel: str, provider_message_id: str
) -> Message:
    """Record the provider id once an adapter accepted the message."""
    with transaction.atomic():
        message = (
            Message.objects.select_for_update()
            .select_related("conversation")
            .filter(workspace_id=workspace_id, pk=message_id)
            .first()
        )
        if message is None:
            raise NotFoundError("message not found")
        message.provider_message_id = provider_message_id
        became_sent = _advances(message.status, MessageStatus.SENT)
        if became_sent:
            message.status = MessageStatus.SENT
        message.save(update_fields=["provider_message_id", "status", "updated_at"])
        if became_sent:
            publish(
                "crm.message.status_changed",
                workspace_id=workspace_id,
                actor=Actor.system("delivery"),
                data=events.MessageStatusChanged(
                    message_id=message.pk,
                    conversation_id=message.conversation_id,
                    contact_id=message.conversation.contact_id,
                    channel=message.channel,
                    status=MessageStatus.SENT,
                ),
            )
    return message


def mirror_outbound(
    *,
    workspace_id: uuid.UUID,
    contact: Contact,
    channel: str,
    provider_message_id: str,
    body: str,
    provider_ts: datetime,
    sent_by: Actor,
    attachments: list[dict[str, Any]] | None = None,
    channel_account_id: uuid.UUID | None = None,
) -> tuple[Message, bool]:
    """Record a message that was sent outside this system (for example by another module)."""
    existing = (
        Message.objects.for_workspace(workspace_id)
        .filter(channel=channel, provider_message_id=provider_message_id)
        .first()
    )
    if existing is not None:
        return existing, False
    with transaction.atomic():
        conversation = get_or_create(workspace_id, contact)
        message = Message.objects.create(
            workspace_id=workspace_id,
            conversation=conversation,
            channel=channel,
            direction=Direction.OUTBOUND,
            channel_account_id=channel_account_id,
            provider_message_id=provider_message_id,
            body=body,
            attachments=attachments or [],
            status=MessageStatus.SENT,
            sent_by_type=sent_by.type,
            sent_by_id=sent_by.id,
            provider_ts=provider_ts,
        )
        touch(conversation, message)
        publish(
            "crm.message.outbound",
            workspace_id=workspace_id,
            actor=sent_by,
            data=events.MessageOutbound(
                message_id=message.pk,
                conversation_id=conversation.pk,
                contact_id=contact.pk,
                channel=channel,
            ),
        )
    return message, True


# ---- internal comments ----------------------------------------------------------------------


def add_comment(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    conversation_id: uuid.UUID,
    body: str,
    mentions: list[uuid.UUID] | None = None,
) -> Message:
    """A team-only note inside the thread. It never reaches the contact."""
    text = body.strip()
    if not text or len(text) > COMMENT_LIMIT:
        raise ValidationFailedError(
            "invalid comment", errors={"body": "required" if not text else "too_long"}
        )
    wanted = set(mentions or [])
    found = set(
        Membership.objects.filter(workspace_id=workspace_id, user_id__in=wanted).values_list(
            "user_id", flat=True
        )
    )
    if wanted != found:
        raise ValidationFailedError("not a member", errors={"mentions": "not_a_member"})
    conversation = get_conversation(workspace_id, conversation_id)
    return Message.objects.create(
        workspace_id=workspace_id,
        conversation=conversation,
        direction=Direction.INTERNAL,
        body=text,
        status=MessageStatus.SENT,
        sent_by_type=actor.type,
        sent_by_id=actor.id,
        mentions=[str(m) for m in sorted(wanted)],
        provider_ts=timezone.now(),
    )


# ---- assignment, state ----------------------------------------------------------------------


def assign(
    *, workspace_id: uuid.UUID, actor: Actor, conversation_id: uuid.UUID, user_id: uuid.UUID | None
) -> Conversation:
    """Reassigning the thread reassigns the contact too, so both always agree."""
    with transaction.atomic():
        conversation = get_conversation(workspace_id, conversation_id)
        contacts.assign(
            workspace_id=workspace_id,
            actor=actor,
            contact_id=conversation.contact_id,
            user_id=user_id,
        )
        conversation.assigned_to_id = user_id
        conversation.save(update_fields=["assigned_to", "updated_at"])
        record_event(
            action="crm.conversation.assigned",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_conversation",
            target_id=conversation.pk,
        )
        publish(
            "crm.conversation.assigned",
            workspace_id=workspace_id,
            actor=actor,
            data=events.ConversationAssigned(
                conversation_id=conversation.pk,
                contact_id=conversation.contact_id,
                assigned_to=user_id,
            ),
        )
    return conversation


def set_flags(
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    starred: bool | None = None,
    status: str | None = None,
) -> Conversation:
    conversation = get_conversation(workspace_id, conversation_id)
    if starred is not None:
        conversation.starred = starred
    if status is not None:
        if status not in ConversationStatus.values:
            raise ValidationFailedError("invalid status", errors={"status": "invalid_value"})
        conversation.status = status
    conversation.save(update_fields=["starred", "status", "updated_at"])
    return conversation


def mark_read(*, workspace_id: uuid.UUID, conversation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    conversation = get_conversation(workspace_id, conversation_id)
    ConversationRead.objects.update_or_create(
        conversation=conversation,
        user_id=user_id,
        defaults={"workspace_id": workspace_id, "read_at": timezone.now()},
    )


def mark_unread(*, workspace_id: uuid.UUID, conversation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    ConversationRead.objects.for_workspace(workspace_id).filter(
        conversation_id=conversation_id, user_id=user_id
    ).delete()
