"""Email through Django's configured mail backend (SMTP, an ESP relay, or the console in dev).

This is the simplest working email channel. Sending domains with verified DNS, bounce handling
and campaigns build on top of the same port in later tasks (CRM-3.3 and following).
"""

from __future__ import annotations

import smtplib

from django.conf import settings

from crm.channels import mime
from crm.consent.models import Channel
from crm.sending.ports import (
    Capabilities,
    OutboundRequest,
    PermanentAdapterError,
    RetryableAdapterError,
    SendResult,
)

MAX_BODY = 200_000
REJECTED = "email_rejected"
UNAVAILABLE = "email_unavailable"


def _domain() -> str:
    address = settings.DEFAULT_FROM_EMAIL
    return address.rsplit("@", 1)[-1].strip("> ") or "localhost"


class SmtpEmailAdapter:
    channel: str = Channel.EMAIL
    capabilities: Capabilities = Capabilities(max_body_length=MAX_BODY, supports_subject=True)

    def send(self, request: OutboundRequest) -> SendResult:
        mail, message_id = mime.build(
            request,
            from_email=request.from_address or settings.DEFAULT_FROM_EMAIL,
            domain=_domain(),
        )
        try:
            mail.send(fail_silently=False)
        except smtplib.SMTPRecipientsRefused as exc:
            raise PermanentAdapterError(REJECTED) from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise RetryableAdapterError(UNAVAILABLE) from exc
        return SendResult(provider_message_id=message_id)
