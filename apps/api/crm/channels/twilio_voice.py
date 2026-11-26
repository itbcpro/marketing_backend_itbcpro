"""Click-to-call through Twilio: ring the agent first, then bridge the customer."""

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
from crm.voice import webhooks

AGENT_KEY = "agent_phone"
NO_AGENT = "voice_no_agent_phone"
MAX_BODY = 500


class TwilioVoiceAdapter:
    channel: str = Channel.VOICE
    capabilities: Capabilities = Capabilities(max_body_length=MAX_BODY)

    def send(self, request: OutboundRequest) -> SendResult:
        agent = next((str(a[AGENT_KEY]) for a in request.attachments if a.get(AGENT_KEY)), "")
        if not agent:
            raise PermanentAdapterError(NO_AGENT)
        try:
            credential = sms.get_credential(request.workspace_id)
        except Exception as exc:  # no account connected is not retryable
            raise PermanentAdapterError(errors.NO_CREDENTIALS) from exc
        sender = request.from_address
        if not sender:
            line = inbox.default_sender(request.workspace_id, Channel.VOICE)
            sender = line.external_id if line else ""
        if not sender:
            raise PermanentAdapterError(errors.NO_SENDER)
        try:
            sent = sms.client_for(credential).call(
                sender=sender,
                to=agent,
                twiml_url=webhooks.callback_url(credential.public_id, "connect"),
                idempotency_key=str(request.message_id),
                status_callback=webhooks.callback_url(credential.public_id, "status"),
            )
        except sms.SmsSendError as exc:
            if exc.retryable:
                raise RetryableAdapterError(exc.code) from exc
            raise PermanentAdapterError(exc.code) from exc
        return SendResult(provider_message_id=sent.provider_message_id)
