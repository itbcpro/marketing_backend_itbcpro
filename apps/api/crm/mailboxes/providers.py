"""Sending through, and reading from, a connected mailbox: SMTP, the Gmail API, Microsoft Graph."""

from __future__ import annotations

import base64
import smtplib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.message import Message as MimeMessage
from typing import Any

import httpx
from django.conf import settings

from crm.mailboxes import http, oauth, smtp
from crm.mailboxes.models import Mailbox, Provider
from crm.mailboxes.sealing import open_credentials

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH = "https://graph.microsoft.com/v1.0/me"
REJECTED = "email_rejected"
UNAVAILABLE = "email_unavailable"
UNAUTHORISED = "mailbox_unauthorised"
SMTP_REFUSED = "email_rejected"
TOO_MANY = 429
SERVER_ERROR = 500


class MailboxSendError(Exception):
    """The mailbox could not send. ``needs_attention`` means the connection itself is broken."""

    def __init__(self, code: str, *, retryable: bool, needs_attention: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.needs_attention = needs_attention


def _failure_of(response: httpx.Response) -> MailboxSendError:
    status = response.status_code
    if status in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
        return MailboxSendError(UNAUTHORISED, retryable=False, needs_attention=True)
    if status == TOO_MANY or status >= SERVER_ERROR:
        return MailboxSendError(UNAVAILABLE, retryable=True)
    return MailboxSendError(REJECTED, retryable=False)


def _authorised(mailbox: Mailbox) -> dict[str, str]:
    try:
        token = oauth.access_token(mailbox)
    except oauth.OAuthError as exc:
        retry = exc.code == "provider_unreachable"
        raise MailboxSendError(
            UNAUTHORISED if not retry else UNAVAILABLE, retryable=retry, needs_attention=not retry
        ) from exc
    return {"Authorization": f"Bearer {token}"}


def send(mailbox: Mailbox, message: MimeMessage) -> None:
    """Send one ready MIME message from the mailbox."""
    if mailbox.provider == Provider.SMTP:
        _send_smtp(mailbox, message)
        return
    raw = message.as_bytes()
    headers = _authorised(mailbox)
    try:
        with http.client() as client:
            if mailbox.provider == Provider.GOOGLE:
                body = base64.urlsafe_b64encode(raw).decode()
                response = client.post(
                    f"{GMAIL}/messages/send", headers=headers, json={"raw": body}
                )
            else:
                response = client.post(
                    f"{GRAPH}/sendMail",
                    headers={**headers, "Content-Type": "text/plain"},
                    content=base64.b64encode(raw),
                )
    except httpx.HTTPError as exc:
        raise MailboxSendError(UNAVAILABLE, retryable=True) from exc
    if response.status_code not in {httpx.codes.OK, httpx.codes.ACCEPTED}:
        raise _failure_of(response)


def _send_smtp(mailbox: Mailbox, message: MimeMessage) -> None:
    config = mailbox.config
    server = smtp.Server(
        host=str(config.get("host", "")),
        port=int(config.get("port", 0)),
        security=str(config.get("security", "")),
        username=str(config.get("username", "")),
    )
    try:
        connection = smtp.connect(server, str(open_credentials(mailbox).get("password", "")))
    except smtp.SmtpError as exc:
        auth = exc.code == "auth_failed"
        raise MailboxSendError(
            UNAUTHORISED if auth else UNAVAILABLE,
            retryable=exc.retryable,
            needs_attention=auth,
        ) from exc
    try:
        connection.send_message(message)
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailboxSendError(SMTP_REFUSED, retryable=False) from exc
    except smtplib.SMTPSenderRefused as exc:
        raise MailboxSendError(UNAUTHORISED, retryable=False, needs_attention=True) from exc
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise MailboxSendError(UNAVAILABLE, retryable=True) from exc
    finally:
        try:
            connection.quit()
        except smtplib.SMTPException, OSError:
            connection.close()


# ---- reading replies --------------------------------------------------------------------


def _epoch_ms(cursor: str) -> int:
    return int(cursor) if cursor.isdigit() else 0


def fetch_new(mailbox: Mailbox) -> Iterator[tuple[bytes, str]]:
    """Raw messages that arrived in the inbox since the cursor, with the cursor after each.

    Messages are yielded in arrival order so a failure part-way keeps the progress made. The
    cursor is inclusive of its own moment; the consumer stores messages idempotently.
    """
    headers = _authorised(mailbox)
    since = _epoch_ms(mailbox.sync_cursor) or int(
        (datetime.now(UTC) - timedelta(days=settings.CRM_MAILBOX_SYNC_DAYS)).timestamp() * 1000
    )
    try:
        with http.client() as client:
            if mailbox.provider == Provider.GOOGLE:
                yield from _gmail(client, headers, since)
            elif mailbox.provider == Provider.MICROSOFT:
                yield from _graph(client, headers, since)
    except httpx.HTTPError as exc:
        raise MailboxSendError(UNAVAILABLE, retryable=True) from exc


def _gmail(
    client: httpx.Client, headers: dict[str, str], since_ms: int
) -> Iterator[tuple[bytes, str]]:
    listing = client.get(
        f"{GMAIL}/messages",
        headers=headers,
        params={
            "q": f"in:inbox after:{since_ms // 1000}",
            "maxResults": settings.CRM_MAILBOX_SYNC_BATCH,
        },
    )
    if listing.status_code != httpx.codes.OK:
        raise _failure_of(listing)
    found: list[tuple[int, bytes]] = []
    for item in listing.json().get("messages", []):
        one = client.get(
            f"{GMAIL}/messages/{item['id']}", headers=headers, params={"format": "raw"}
        )
        if one.status_code != httpx.codes.OK:
            raise _failure_of(one)
        body: dict[str, Any] = one.json()
        raw = base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4))
        found.append((int(body.get("internalDate", 0)), raw))
    for moment, raw in sorted(found, key=lambda pair: pair[0]):
        yield raw, str(moment)


def _graph(
    client: httpx.Client, headers: dict[str, str], since_ms: int
) -> Iterator[tuple[bytes, str]]:
    since = datetime.fromtimestamp(since_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    listing = client.get(
        f"{GRAPH}/mailFolders/inbox/messages",
        headers=headers,
        params={
            "$filter": f"receivedDateTime ge {since}",
            "$orderby": "receivedDateTime asc",
            "$select": "id,receivedDateTime",
            "$top": settings.CRM_MAILBOX_SYNC_BATCH,
        },
    )
    if listing.status_code != httpx.codes.OK:
        raise _failure_of(listing)
    for item in listing.json().get("value", []):
        one = client.get(f"{GRAPH}/messages/{item['id']}/$value", headers=headers)
        if one.status_code != httpx.codes.OK:
            raise _failure_of(one)
        received = datetime.fromisoformat(item["receivedDateTime"].replace("Z", "+00:00"))
        yield one.content, str(int(received.timestamp() * 1000))
