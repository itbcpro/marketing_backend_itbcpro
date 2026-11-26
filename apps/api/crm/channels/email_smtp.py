"""Email through Django's configured mail backend (SMTP, an ESP relay, or the console in dev).

This is the simplest working email channel. Sending domains with verified DNS, bounce handling
and campaigns build on top of the same port in later tasks (CRM-3.3 and following).
"""

from __future__ import annotations

import smtplib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.mail.message import make_msgid

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
        # The Message-ID is derived from our message id, so a retry reuses it and mail clients
        # and servers can drop the duplicate.
        message_id = make_msgid(idstring=request.message_id.hex, domain=_domain())
        headers = {"Message-ID": message_id}
        if request.in_reply_to:
            headers["In-Reply-To"] = request.in_reply_to
            headers["References"] = request.in_reply_to
        if request.unsubscribe_url:
            # RFC 8058: mail clients show an unsubscribe button and POST to this link.
            headers["List-Unsubscribe"] = f"<{request.unsubscribe_url}>"
            headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        mail = EmailMultiAlternatives(
            subject=request.subject or "(no subject)",
            body=request.body,
            from_email=request.from_address or settings.DEFAULT_FROM_EMAIL,
            to=[request.to],
            headers=headers,
        )
        if request.html:
            mail.attach_alternative(request.html, "text/html")
        try:
            mail.send(fail_silently=False)
        except smtplib.SMTPRecipientsRefused as exc:
            raise PermanentAdapterError(REJECTED) from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise RetryableAdapterError(UNAVAILABLE) from exc
        return SendResult(provider_message_id=message_id)
