"""Build the outgoing email message, the same way for every route it may leave by."""

from __future__ import annotations

from django.core.mail import EmailMultiAlternatives
from django.core.mail.message import make_msgid

from crm.sending.ports import OutboundRequest


def build(
    request: OutboundRequest, *, from_email: str, domain: str
) -> tuple[EmailMultiAlternatives, str]:
    """The message and its Message-ID.

    The Message-ID derives from our message id, so a retry reuses it and mail servers and clients
    can drop the duplicate.
    """
    message_id = make_msgid(idstring=request.message_id.hex, domain=domain)
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
        from_email=from_email,
        to=[request.to],
        headers=headers,
    )
    if request.html:
        mail.attach_alternative(request.html, "text/html")
    return mail, message_id
