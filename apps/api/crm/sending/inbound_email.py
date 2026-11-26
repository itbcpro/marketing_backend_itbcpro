"""Parse a raw inbound email and route it: a real message, an auto-reply, or a bounce report."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Literal

from django.utils import timezone
from django.utils.html import strip_tags

from core.errors import ValidationFailedError
from crm.common.normalize import InvalidValueError, normalise_email
from crm.consent.models import Channel
from crm.inbox import services as inbox
from crm.sending import feedback, suppression
from crm.sending.models import SuppressionReason

Kind = Literal["message", "auto_reply", "bounce"]
MAX_BODY = 100_000
HARD_STATUS_PREFIX = "5"
BULK_PRECEDENCE = {"bulk", "junk", "list", "auto_reply"}
AUTO_SUBJECTS = ("automatic reply", "auto-reply", "autoreply", "out of office")
BOUNCE_SENDERS = ("mailer-daemon", "postmaster")
QUOTE_MARKERS = ("-----original message-----", "________________________________")


@dataclass
class ParsedEmail:
    kind: Kind
    message_id: str = ""
    in_reply_to: str = ""
    sender: str = ""
    sender_name: str = ""
    subject: str = ""
    body: str = ""
    sent_at: datetime = field(default_factory=timezone.now)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    bounced_recipient: str = ""
    bounced_message_id: str = ""
    hard_bounce: bool = False


def strip_quoted(text: str) -> str:
    """Keep what the person wrote; drop the quoted history below it."""
    kept: list[str] = []
    for line in text.splitlines():
        lowered = line.strip().lower()
        starts_reply_header = lowered.startswith("on ") and lowered.endswith("wrote:")
        if starts_reply_header or lowered in QUOTE_MARKERS:
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = strip_tags(text)
    return strip_quoted(str(text))[:MAX_BODY]


def _attachments(message: EmailMessage) -> list[dict[str, Any]]:
    return [
        {
            "filename": part.get_filename() or "",
            "content_type": part.get_content_type(),
            "size": len(part.get_content() or b"") if part.get_content_maintype() != "text" else 0,
        }
        for part in message.iter_attachments()
        if part.get_content_type() not in {"message/delivery-status", "message/rfc822"}
    ]


def _is_bounce(message: EmailMessage, sender: str) -> bool:
    report = message.get_content_type() == "multipart/report"
    delivery_report = report and message.get_param("report-type") == "delivery-status"
    local_part = sender.split("@", maxsplit=1)[0].lower()
    return bool(delivery_report) or local_part in BOUNCE_SENDERS


def _is_auto_reply(message: EmailMessage) -> bool:
    auto = str(message.get("Auto-Submitted", "no")).lower() != "no"
    bulk = str(message.get("Precedence", "")).lower() in BULK_PRECEDENCE
    flagged = any(message.get(h) for h in ("X-Autoreply", "X-Autorespond"))
    subject = str(message.get("Subject", "")).lower()
    return auto or bulk or flagged or subject.startswith(AUTO_SUBJECTS)


def _sub_messages(part: Message) -> list[Message]:
    """The parsed messages inside a delivery-status or rfc822 part."""
    payload = part.get_payload()
    return [m for m in payload if isinstance(m, Message)] if isinstance(payload, list) else []


def _bounce_details(message: EmailMessage) -> tuple[str, str, bool]:
    recipient, original_id, hard = "", "", False
    for part in message.walk():
        kind = part.get_content_type()
        if kind == "message/delivery-status":
            for block in _sub_messages(part):
                if str(block.get("Action", "")).lower() == "failed":
                    final = str(block.get("Final-Recipient", ""))
                    recipient = recipient or final.split(";", 1)[-1].strip()
                    hard = str(block.get("Status", "")).startswith(HARD_STATUS_PREFIX)
        elif kind in {"message/rfc822", "text/rfc822-headers"}:
            for inner in _sub_messages(part)[:1]:
                original_id = original_id or str(inner.get("Message-ID", "")).strip()
    return recipient, original_id, hard


def _sent_at(message: EmailMessage) -> datetime:
    try:
        moment = parsedate_to_datetime(str(message.get("Date", "")))
    except TypeError, ValueError:
        return timezone.now()
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse(raw: bytes) -> ParsedEmail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    name, address = parseaddr(str(message.get("From", "")))
    sender = address.lower()
    if _is_bounce(message, sender):
        recipient, original_id, hard = _bounce_details(message)
        return ParsedEmail(
            kind="bounce",
            sender=sender,
            bounced_recipient=recipient.lower(),
            bounced_message_id=original_id,
            hard_bounce=hard,
        )
    return ParsedEmail(
        kind="auto_reply" if _is_auto_reply(message) else "message",
        message_id=str(message.get("Message-ID", "")).strip(),
        in_reply_to=str(message.get("In-Reply-To", "")).strip(),
        sender=sender,
        sender_name=name,
        subject=str(message.get("Subject", "")).strip()[:300],
        body=_body(message),
        sent_at=_sent_at(message),
        attachments=_attachments(message),
    )


def ingest(*, workspace_id: uuid.UUID, raw: bytes) -> tuple[Kind, Any]:
    """Process one raw email. Returns what it was and the stored message, if any."""
    parsed = parse(raw)
    if parsed.kind == "bounce":
        message = (
            feedback.record_bounce(
                workspace_id=workspace_id,
                channel=Channel.EMAIL,
                provider_message_id=parsed.bounced_message_id,
                hard=parsed.hard_bounce,
            )
            if parsed.bounced_message_id
            else None
        )
        if message is None and parsed.hard_bounce and parsed.bounced_recipient:
            suppression.suppress(
                workspace_id=workspace_id,
                actor=feedback.SYSTEM,
                channel=Channel.EMAIL,
                address=parsed.bounced_recipient,
                reason=SuppressionReason.HARD_BOUNCE,
            )
        return "bounce", message
    if parsed.kind == "auto_reply":
        return "auto_reply", None  # never counts as the customer answering
    try:
        normalise_email(parsed.sender)
    except InvalidValueError as exc:
        raise ValidationFailedError("invalid sender", errors={"sender": str(exc)}) from exc
    provider_id = parsed.message_id or f"sha256:{hashlib.sha256(raw).hexdigest()}"
    result = inbox.ingest(
        workspace_id=workspace_id,
        item=inbox.InboundMessage(
            channel=Channel.EMAIL,
            provider_message_id=provider_id,
            sender_channel="email",
            sender_value=parsed.sender,
            provider_ts=parsed.sent_at,
            body=parsed.body,
            subject=parsed.subject,
            attachments=parsed.attachments,
            sender_name=parsed.sender_name,
        ),
    )
    return "message", result.message
