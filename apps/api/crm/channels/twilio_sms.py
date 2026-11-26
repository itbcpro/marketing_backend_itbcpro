"""SMS and MMS through the workspace's own Twilio account."""

from __future__ import annotations

from crm.consent.models import Channel
from crm.inbox import services as inbox
from crm.sending.ports import (
    Capabilities,
    OutboundRequest,
    PermanentAdapterError,
    RetryableAdapterError,
    SendResult,
)
from crm.sms import errors
from crm.sms import services as sms

MAX_BODY = 1600  # Twilio's limit for one message, however many segments it needs.


class TwilioSmsAdapter:
    channel: str = Channel.SMS
    capabilities: Capabilities = Capabilities(max_body_length=MAX_BODY, supports_attachments=True)

    def send(self, request: OutboundRequest) -> SendResult:
        try:
            credential = sms.get_credential(request.workspace_id)
        except Exception as exc:  # no account connected is not retryable
            raise PermanentAdapterError(errors.NO_CREDENTIALS) from exc
        sender = request.from_address
        if not sender:
            account = inbox.default_sender(request.workspace_id, Channel.SMS)
            sender = account.external_id if account else ""
        if not sender:
            raise PermanentAdapterError(errors.NO_SENDER)
        media = [str(a["url"]) for a in request.attachments if a.get("url")]
        try:
            sent = sms.client_for(credential).send(
                sender=sender,
                to=request.to,
                body=request.body,
                media_urls=media,
                # The same message always carries the same token, so a retry never sends twice.
                idempotency_key=str(request.message_id),
                status_callback=sms.callback_url(credential, "status"),
            )
        except sms.SmsSendError as exc:
            if exc.retryable:
                raise RetryableAdapterError(exc.code) from exc
            raise PermanentAdapterError(exc.code) from exc
        return SendResult(provider_message_id=sent.provider_message_id)
