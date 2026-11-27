"""Sign-in with Google or Microsoft (authorization code flow with PKCE) and token refresh.

The operator registers one OAuth app per provider and sets its client id and secret in the
environment; a provider without them is not offered. Each sign-in is a single-use request whose
``state`` is stored only as a hash, bound to the person who started it, and valid for minutes.
Refresh tokens live in the vault. Nothing here is logged with a token in it.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.utils import timezone

from core import tenancy
from crm.mailboxes import http
from crm.mailboxes.models import AuthRequest, Mailbox, MailboxStatus, Provider
from crm.mailboxes.sealing import open_credentials, open_verifier, seal_credentials, seal_verifier

EXPIRY_MARGIN = timedelta(seconds=60)
VERIFIER_BYTES = 48
STATE_BYTES = 32


class OAuthError(Exception):
    """A sign-in or refresh failed. ``code`` is stable and safe to show."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Spec:
    authorize: str
    token: str
    identity: str
    send_scopes: tuple[str, ...]
    sync_scopes: tuple[str, ...]
    extra: dict[str, str]


def _microsoft(path: str) -> str:
    return f"https://login.microsoftonline.com/{settings.CRM_MICROSOFT_TENANT}/oauth2/v2.0/{path}"


def spec_for(provider: str) -> Spec:
    if provider == Provider.GOOGLE:
        return Spec(
            authorize="https://accounts.google.com/o/oauth2/v2/auth",
            token="https://oauth2.googleapis.com/token",  # noqa: S106 - a public URL
            identity="https://openidconnect.googleapis.com/v1/userinfo",
            send_scopes=(
                "openid",
                "email",
                "profile",
                "https://www.googleapis.com/auth/gmail.send",
            ),
            sync_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            extra={"access_type": "offline", "prompt": "consent"},
        )
    if provider == Provider.MICROSOFT:
        return Spec(
            authorize=_microsoft("authorize"),
            token=_microsoft("token"),
            identity="https://graph.microsoft.com/v1.0/me",
            send_scopes=("openid", "email", "profile", "offline_access", "User.Read", "Mail.Send"),
            sync_scopes=("Mail.Read",),
            extra={"prompt": "select_account"},
        )
    raise OAuthError("unknown_provider")


def _credentials_of(provider: str) -> tuple[str, str]:
    if provider == Provider.GOOGLE:
        return settings.CRM_GOOGLE_CLIENT_ID, settings.CRM_GOOGLE_CLIENT_SECRET
    return settings.CRM_MICROSOFT_CLIENT_ID, settings.CRM_MICROSOFT_CLIENT_SECRET


def configured(provider: str) -> bool:
    client_id, client_secret = _credentials_of(provider)
    return bool(client_id and client_secret and settings.CRM_MAILBOX_REDIRECT_URI)


def available() -> list[str]:
    """The providers this deployment can connect."""
    return [p.value for p in (Provider.GOOGLE, Provider.MICROSOFT) if configured(p.value)]


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def scopes_for(provider: str, *, sync: bool) -> list[str]:
    spec = spec_for(provider)
    return [*spec.send_scopes, *(spec.sync_scopes if sync else ())]


def start(
    *, workspace_id: uuid.UUID, user_id: uuid.UUID, provider: str, sync: bool, shared: bool
) -> str:
    """Begin a sign-in. Returns the address the person's browser should go to."""
    if not configured(provider):
        raise OAuthError("not_configured")
    spec = spec_for(provider)
    state = f"{workspace_id.hex}.{secrets.token_urlsafe(STATE_BYTES)}"
    verifier = secrets.token_urlsafe(VERIFIER_BYTES)
    request = AuthRequest(
        workspace_id=workspace_id,
        state_hash=_hash(state),
        user_id=user_id,
        provider=provider,
        sync=sync,
        shared=shared,
        expires_at=timezone.now() + timedelta(seconds=settings.CRM_MAILBOX_OAUTH_TTL_SECONDS),
    )
    seal_verifier(request, verifier)
    request.save()
    client_id, _ = _credentials_of(provider)
    query = {
        "client_id": client_id,
        "redirect_uri": settings.CRM_MAILBOX_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes_for(provider, sync=sync)),
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        **spec.extra,
    }
    return f"{spec.authorize}?{urlencode(query)}"


def _post_token(provider: str, form: dict[str, str]) -> dict[str, Any]:
    client_id, client_secret = _credentials_of(provider)
    data = {"client_id": client_id, "client_secret": client_secret, **form}
    try:
        with http.client() as client:
            response = client.post(spec_for(provider).token, data=data)
    except httpx.HTTPError as exc:
        raise OAuthError("provider_unreachable") from exc
    body = response.json() if response.content else {}
    if response.status_code != httpx.codes.OK:
        code = "invalid_grant" if body.get("error") == "invalid_grant" else "token_refused"
        raise OAuthError(code)
    return dict(body)


def _identity(provider: str, access_token: str) -> tuple[str, str]:
    """The email address and display name the grant belongs to."""
    try:
        with http.client() as client:
            response = client.get(
                spec_for(provider).identity, headers={"Authorization": f"Bearer {access_token}"}
            )
    except httpx.HTTPError as exc:
        raise OAuthError("provider_unreachable") from exc
    if response.status_code != httpx.codes.OK:
        raise OAuthError("identity_refused")
    body = response.json()
    email = body.get("email") or body.get("mail") or body.get("userPrincipalName") or ""
    return str(email).strip().lower(), str(body.get("name") or body.get("displayName") or "")


@dataclass(frozen=True)
class Grant:
    """What a finished sign-in yields, before it becomes a mailbox."""

    workspace_id: uuid.UUID
    user_id: uuid.UUID
    provider: str
    email: str
    display_name: str
    sync: bool
    shared: bool
    credentials: dict[str, Any]
    scopes: list[str]


def _tokens(body: dict[str, Any], previous_refresh: str = "") -> dict[str, Any]:
    expires = timezone.now() + timedelta(seconds=int(body.get("expires_in", 3600)))
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token") or previous_refresh,
        "expires_at": expires.isoformat(),
    }


def finish(*, state: str, code: str) -> Grant:
    """Complete a sign-in from the provider's redirect. A state works once."""
    prefix, _, _ = state.partition(".")
    try:
        workspace_id = uuid.UUID(hex=prefix)
    except ValueError as exc:
        raise OAuthError("invalid_state") from exc
    with tenancy.use_workspace(workspace_id):
        request = (
            AuthRequest.objects.for_workspace(workspace_id).filter(state_hash=_hash(state)).first()
        )
        if request is None or request.expires_at < timezone.now():
            raise OAuthError("invalid_state")
        verifier, provider = open_verifier(request), request.provider
        sync, shared, user_id = request.sync, request.shared, request.user_id
        request.delete()  # single use, even when the exchange below fails
    body = _post_token(
        provider,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.CRM_MAILBOX_REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    tokens = _tokens(body)
    if not tokens["refresh_token"]:
        raise OAuthError("no_refresh_token")
    email, name = _identity(provider, tokens["access_token"])
    if not email:
        raise OAuthError("identity_refused")
    granted = str(body.get("scope", "")).split()
    return Grant(
        workspace_id,
        user_id,
        provider,
        email,
        name,
        sync,
        shared,
        tokens,
        granted or scopes_for(provider, sync=sync),
    )


def access_token(mailbox: Mailbox) -> str:
    """A valid access token, refreshed (and saved) when it is about to expire."""
    credentials = open_credentials(mailbox)
    expires = credentials.get("expires_at")
    if (
        credentials.get("access_token")
        and expires
        and datetime.fromisoformat(expires) - EXPIRY_MARGIN > timezone.now()
    ):
        return str(credentials["access_token"])
    refresh = str(credentials.get("refresh_token", ""))
    if not refresh:
        raise OAuthError("invalid_grant")
    try:
        body = _post_token(
            mailbox.provider, {"grant_type": "refresh_token", "refresh_token": refresh}
        )
    except OAuthError as exc:
        if exc.code == "invalid_grant":
            mailbox.status, mailbox.last_error = MailboxStatus.NEEDS_ATTENTION, "auth_failed"
            mailbox.save(update_fields=["status", "last_error", "updated_at"])
        raise
    fresh = _tokens(body, previous_refresh=refresh)
    seal_credentials(mailbox, fresh)
    mailbox.save(
        update_fields=[
            "secret_ciphertext",
            "secret_nonce",
            "secret_wrapped_key",
            "secret_wrapped_key_nonce",
            "secret_key_version",
            "updated_at",
        ]
    )
    return str(fresh["access_token"])


def revoke(mailbox: Mailbox) -> None:
    """Ask Google to drop the grant. Best effort: a failure must not block disconnecting."""
    if mailbox.provider != Provider.GOOGLE:
        return
    token = str(open_credentials(mailbox).get("refresh_token", ""))
    if not token:
        return
    try:
        with http.client() as client:
            client.post("https://oauth2.googleapis.com/revoke", data={"token": token})
    except httpx.HTTPError:
        return
