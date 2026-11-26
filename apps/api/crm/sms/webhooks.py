"""Twilio callbacks: replies from customers and delivery reports for what we sent."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from core import tenancy
from core.errors import NotFoundError, ValidationFailedError
from crm.consent.models import Channel
from crm.inbox import services as inbox
from crm.sms import errors, services

# Twilio statuses that mean something to us; queued, accepted and sending are not reported on.
STATUS_MAP = {"sent": "sent", "delivered": "delivered", "undelivered": "failed", "failed": "failed"}
MAX_MEDIA = 10


def receive(public_id: str, kind: str, signature: str, params: dict[str, str]) -> None:
    credential = services.credential_for(public_id)
    services.verify_signature(
        credential, services.callback_url(credential, kind), signature, params
    )
    with tenancy.use_workspace(credential.workspace_id):
        if kind == "status":
            _status(credential.workspace_id, params)
        elif kind == "inbound":
            _inbound(credential.workspace_id, params)
        else:
            raise NotFoundError("not found")


def _status(workspace_id: uuid.UUID, params: dict[str, str]) -> None:
    status = STATUS_MAP.get(params.get("MessageStatus", ""))
    sid = params.get("MessageSid", "")
    if status is None or not sid:
        return
    code = params.get("ErrorCode", "")
    inbox.apply_status(
        workspace_id=workspace_id,
        channel=Channel.SMS,
        provider_message_id=sid,
        status=status,
        provider_ts=datetime.now(UTC),
        error_code=errors.from_twilio(int(code) if code.isdigit() else None, 0).code
        if status == "failed"
        else "",
    )


def _inbound(workspace_id: uuid.UUID, params: dict[str, str]) -> None:
    sid, sender = params.get("MessageSid", ""), params.get("From", "")
    if not sid or not sender:
        raise ValidationFailedError("invalid_message")
    media_count = min(int(params.get("NumMedia", "0") or 0), MAX_MEDIA)
    attachments = [
        {"url": params[f"MediaUrl{i}"], "content_type": params.get(f"MediaContentType{i}", "")}
        for i in range(media_count)
        if params.get(f"MediaUrl{i}")
    ]
    receiver = inbox.find_account(workspace_id, Channel.SMS, params.get("To", ""))
    inbox.ingest(
        workspace_id=workspace_id,
        item=inbox.InboundMessage(
            channel=Channel.SMS,
            provider_message_id=sid,
            sender_channel="phone",
            sender_value=sender,
            provider_ts=datetime.now(UTC),
            body=params.get("Body", ""),
            attachments=attachments,
            channel_account_id=receiver.pk if receiver else None,
        ),
    )
