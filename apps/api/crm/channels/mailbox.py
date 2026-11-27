"""Email sent from a connected mailbox (SMTP login, Gmail, Microsoft 365) as its owner."""

from __future__ import annotations

from email.utils import formataddr
from typing import Any

from crm.channels import mime
from crm.consent.models import Channel
from crm.mailboxes import providers, services
from crm.sending.ports import (
    Capabilities,
    OutboundRequest,
    PermanentAdapterError,
    RetryableAdapterError,
    SendResult,
)

MAX_BODY = 200_000


class MailboxAdapter:
    channel: str = Channel.EMAIL
    capabilities: Capabilities = Capabilities(max_body_length=MAX_BODY, supports_subject=True)

    def __init__(self, account: Any) -> None:
        self._account = account

    def send(self, request: OutboundRequest) -> SendResult:
        mailbox = services.mailbox_of(request.workspace_id, self._account)
        sender = (
            formataddr((mailbox.display_name, mailbox.email))
            if mailbox.display_name
            else mailbox.email
        )
        mail, message_id = mime.build(
            request, from_email=sender, domain=mailbox.email.rsplit("@", 1)[-1]
        )
        try:
            providers.send(mailbox, mail.message())
        except providers.MailboxSendError as failure:
            services.mark_failed(mailbox, failure)
            if failure.retryable:
                raise RetryableAdapterError(failure.code) from failure
            raise PermanentAdapterError(failure.code) from failure
        services.mark_sent(mailbox)
        return SendResult(provider_message_id=message_id)
