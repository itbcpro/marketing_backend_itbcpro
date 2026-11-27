"""Hand a queued message to its channel adapter and record the result."""

from __future__ import annotations

import uuid
from typing import Any

from core.errors import NotFoundError
from crm.consent.models import Channel
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.inbox.models import ChannelAccount, Direction, Message, MessageStatus
from crm.ops import metrics
from crm.sending import registry, unsubscribe
from crm.sending.ports import AdapterError, OutboundRequest, RetryableAdapterError
from crm.sending.rules import ADDRESS_IDENTITY, Block
from crm.tracking import services as tracking


def _reply_target(workspace_id: uuid.UUID, message: Message) -> str:
    """Provider id of the last message the contact sent on this channel, for reply threading."""
    last = (
        Message.objects.for_workspace(workspace_id)
        .filter(
            conversation=message.conversation, channel=message.channel, direction=Direction.INBOUND
        )
        .exclude(provider_message_id="")
        .order_by("-provider_ts", "-pk")
        .values_list("provider_message_id", flat=True)
        .first()
    )
    return last or ""


def _sent(**kwargs: Any) -> Message:
    message = inbox.mark_sent(**kwargs)
    metrics.message_outcome(message.channel, "sent")
    return message


def _failed(**kwargs: Any) -> Message:
    message = inbox.set_failed(**kwargs)
    metrics.message_outcome(message.channel, "failed")
    return message


def deliver(*, workspace_id: uuid.UUID, message_id: uuid.UUID, final_attempt: bool) -> Message:
    """Send one pending message; retryable failures re-raise unless it is the last attempt."""
    message = (
        Message.objects.for_workspace(workspace_id)
        .select_related("conversation__contact")
        .filter(pk=message_id, direction=Direction.OUTBOUND)
        .first()
    )
    if message is None:
        raise NotFoundError("message not found")
    if message.status != MessageStatus.PENDING:
        return message  # already handled: a retry after success must not send twice
    if message.provider_message_id:  # the provider took it but we crashed before recording
        return _sent(
            workspace_id=workspace_id,
            message_id=message.pk,
            channel=message.channel,
            provider_message_id=message.provider_message_id,
        )
    contact = message.conversation.contact
    account = (
        ChannelAccount.objects.for_workspace(workspace_id)
        .filter(pk=message.channel_account_id)
        .first()
        if message.channel_account_id
        else None
    )
    adapter = (
        registry.adapter_for_account(account.provider, account) if account else None
    ) or registry.adapter_for(message.channel)
    to = contacts.identity_value(
        workspace_id, contact.pk, ADDRESS_IDENTITY.get(message.channel, "")
    )
    if adapter is None or not to:
        return _failed(
            workspace_id=workspace_id,
            message_id=message.pk,
            error_code=Block.NO_CHANNEL if adapter is None else Block.NO_ADDRESS,
        )
    body, page = message.body, ""
    if message.channel == Channel.EMAIL and (
        message.options.get(tracking.OPTION_OPENS) or message.options.get(tracking.OPTION_CLICKS)
    ):
        decorated = tracking.decorate(
            workspace_id,
            message.pk,
            contact.pk,
            message.body,
            opens=bool(message.options.get(tracking.OPTION_OPENS)),
            clicks=bool(message.options.get(tracking.OPTION_CLICKS)),
        )
        body, page = decorated.text, decorated.html
    request = OutboundRequest(
        workspace_id=workspace_id,
        message_id=message.pk,
        channel=message.channel,
        to=to,
        body=body,
        html=page,
        subject=message.subject,
        attachments=message.attachments,
        from_address=account.external_id if account else "",
        in_reply_to=_reply_target(workspace_id, message),
        unsubscribe_url=(
            unsubscribe.url_for(workspace_id, contact.pk, message.channel)
            if message.channel == Channel.EMAIL
            else ""
        ),
    )
    try:
        result = adapter.send(request)
    except RetryableAdapterError as error:
        if not final_attempt:
            raise
        return _failed(workspace_id=workspace_id, message_id=message.pk, error_code=error.code)
    except AdapterError as error:
        return _failed(workspace_id=workspace_id, message_id=message.pk, error_code=error.code)
    return _sent(
        workspace_id=workspace_id,
        message_id=message.pk,
        channel=message.channel,
        provider_message_id=result.provider_message_id,
    )
