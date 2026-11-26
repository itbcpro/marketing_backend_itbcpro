"""Inbound pipeline: normalise, resolve the contact, store once, update the thread, announce."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction

from audit.actors import Actor
from core.errors import ConflictError, ValidationFailedError
from crm.common.normalize import InvalidValueError, normalise_email, normalise_phone
from crm.consent import services as consent
from crm.consent.keywords import KeywordAction
from crm.consent.models import Channel
from crm.contacts import services as contacts
from crm.contacts.models import Contact, IdentityChannel
from crm.events.publisher import publish
from crm.inbox import conversations, events
from crm.inbox.models import Direction, Message, MessageStatus

# Channels where a STOP or START style message changes consent.
KEYWORD_CHANNELS = frozenset({Channel.SMS, Channel.WHATSAPP, Channel.MESSENGER, Channel.INSTAGRAM})
SOURCE_LABEL_LENGTH = 100


@dataclass
class InboundMessage:
    """What an adapter hands over after verifying and parsing a provider delivery."""

    channel: str
    provider_message_id: str
    sender_channel: str
    sender_value: str
    provider_ts: datetime
    body: str = ""
    subject: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    sender_name: str = ""
    channel_account_id: uuid.UUID | None = None


@dataclass
class IngestResult:
    message: Message
    created: bool
    contact: Contact
    keyword: KeywordAction | None = None


def _normalise(sender_channel: str, value: str) -> str:
    try:
        if sender_channel == IdentityChannel.EMAIL:
            return normalise_email(value)
        if sender_channel == IdentityChannel.PHONE:
            return normalise_phone(value)
    except InvalidValueError as exc:
        raise ValidationFailedError("invalid sender", errors={"sender": str(exc)}) from exc
    cleaned = value.strip()
    if not cleaned:
        raise ValidationFailedError("invalid sender", errors={"sender": "required"})
    return cleaned


def resolve_contact(
    workspace_id: uuid.UUID, *, channel: str, sender_channel: str, value: str, name: str = ""
) -> Contact:
    """The contact owning this identity, created on first contact (``value`` is normalised)."""
    found = contacts.find_contact(workspace_id, sender_channel, value)
    if found is not None:
        return found
    first, _, last = name.strip().partition(" ")
    data: dict[str, Any] = {"first_name": first, "last_name": last, "source": channel}
    if sender_channel in {IdentityChannel.EMAIL, IdentityChannel.PHONE}:
        data[str(sender_channel)] = value
    actor = Actor.system("inbound")
    try:
        contact = contacts.create_contact(workspace_id=workspace_id, actor=actor, data=data)
    except ConflictError:  # created by a parallel delivery between our lookup and insert
        racing = contacts.find_contact(workspace_id, sender_channel, value)
        if racing is None:
            raise
        return racing
    if sender_channel not in {IdentityChannel.EMAIL, IdentityChannel.PHONE}:
        contacts.attach_identity(contact, sender_channel, value)
    return contact


def _existing(workspace_id: uuid.UUID, item: InboundMessage) -> Message | None:
    return (
        Message.objects.for_workspace(workspace_id)
        .filter(channel=item.channel, provider_message_id=item.provider_message_id)
        .select_related("conversation__contact")
        .first()
    )


def ingest(*, workspace_id: uuid.UUID, item: InboundMessage) -> IngestResult:
    """Store one inbound message exactly once, however many times the provider delivers it."""
    if item.channel not in Channel.values or not item.provider_message_id:
        raise ValidationFailedError("invalid message", errors={"message": "invalid_value"})
    duplicate = _existing(workspace_id, item)
    if duplicate is not None:
        return IngestResult(duplicate, False, duplicate.conversation.contact)
    value = _normalise(item.sender_channel, item.sender_value)
    contact = resolve_contact(
        workspace_id,
        channel=item.channel,
        sender_channel=item.sender_channel,
        value=value,
        name=item.sender_name,
    )
    try:
        with transaction.atomic():
            conversation = conversations.get_or_create(workspace_id, contact)
            message = Message.objects.create(
                workspace_id=workspace_id,
                conversation=conversation,
                channel=item.channel,
                direction=Direction.INBOUND,
                channel_account_id=item.channel_account_id,
                provider_message_id=item.provider_message_id,
                subject=item.subject,
                body=item.body,
                attachments=item.attachments,
                status=MessageStatus.RECEIVED,
                sent_by_type="contact",
                sent_by_id=str(contact.pk),
                provider_ts=item.provider_ts,
            )
            conversations.touch(conversation, message)
            publish(
                "crm.message.inbound",
                workspace_id=workspace_id,
                actor=Actor.system(item.channel),
                data=events.MessageInbound(
                    message_id=message.pk,
                    conversation_id=conversation.pk,
                    contact_id=contact.pk,
                    channel=item.channel,
                ),
            )
            keyword = (
                consent.handle_keyword(
                    workspace_id=workspace_id,
                    contact_id=contact.pk,
                    channel=item.channel,
                    text=item.body,
                    message_id=item.provider_message_id,
                )
                if item.channel in KEYWORD_CHANNELS
                else None
            )
    except IntegrityError:  # the same delivery won the race
        duplicate = _existing(workspace_id, item)
        if duplicate is None:
            raise
        return IngestResult(duplicate, False, duplicate.conversation.contact)
    return IngestResult(message, True, contact, keyword)
