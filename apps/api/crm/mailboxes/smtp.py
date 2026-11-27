"""SMTP mailboxes: a guarded connection, and a login test that sends nothing.

A person supplies the host, so the server must not be tricked into reaching its own network. The
host is resolved and every address checked immediately before each connection; only well-known
mail ports are allowed; TLS is mandatory (``ssl`` on connect, or ``starttls`` that must succeed);
certificates are verified. Passwords never leave the vault except to log in.
"""

from __future__ import annotations

import ipaddress
import smtplib
import socket
import ssl
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from core.errors import ValidationFailedError

PORTS = (25, 465, 587, 2525)
SECURITIES = ("ssl", "starttls")
TIMEOUT_SECONDS = 15
HOST_LIMIT = 253


class SmtpError(Exception):
    """A connection or login failed. ``code`` is stable; ``retryable`` says whether to try later."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    security: str
    username: str


def clean_server(raw: dict[str, Any]) -> Server:
    """Validate the non-secret half of an SMTP setup."""
    host = str(raw.get("host", "")).strip().lower().rstrip(".")
    errors: dict[str, str] = {}
    if not host or len(host) > HOST_LIMIT or any(c in host for c in "/@ :"):
        errors["host"] = "invalid_host"
    try:
        port = int(raw.get("port", 0))
    except TypeError, ValueError:
        port = 0
    if port not in PORTS:
        errors["port"] = "invalid_port"
    security = str(raw.get("security", ""))
    if security not in SECURITIES:
        errors["security"] = "invalid_security"
    username = str(raw.get("username", "")).strip()
    if not username or len(username) > 254:  # noqa: PLR2004 - the length of an email address
        errors["username"] = "required"
    if errors:
        raise ValidationFailedError("invalid smtp settings", errors=errors)
    return Server(host, port, security, username)


def check_host(host: str) -> None:
    """Refuse a host that is, or resolves to, an address on a private or special network."""
    if settings.CRM_MAILBOX_ALLOW_PRIVATE_HOSTS:
        return
    try:
        found = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SmtpError("host_unresolvable", retryable=True) from exc
    for entry in found:
        address = ipaddress.ip_address(str(entry[4][0]).split("%", 1)[0])
        if not address.is_global:
            raise SmtpError("host_not_allowed")


def connect(server: Server, password: str) -> smtplib.SMTP:
    """An authenticated connection. The caller must ``quit`` it."""
    check_host(server.host)
    context = ssl.create_default_context()
    try:
        if server.security == "ssl":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                server.host, server.port, timeout=TIMEOUT_SECONDS, context=context
            )
        else:
            smtp = smtplib.SMTP(server.host, server.port, timeout=TIMEOUT_SECONDS)
            smtp.ehlo()
            if not smtp.has_extn("starttls"):
                smtp.close()
                raise SmtpError("starttls_unsupported")
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(server.username, password)
    except smtplib.SMTPAuthenticationError as exc:
        raise SmtpError("auth_failed") from exc
    except ssl.SSLError as exc:
        raise SmtpError("tls_failed") from exc
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise SmtpError("unreachable", retryable=True) from exc
    return smtp


def test_login(server: Server, password: str) -> None:
    """Connect and log in, then hang up. Raises :class:`SmtpError` with the reason."""
    smtp = connect(server, password)
    try:
        smtp.noop()
    finally:
        try:
            smtp.quit()
        except smtplib.SMTPException, OSError:
            smtp.close()
