"""Token steps of Instagram Login: code exchange, long-lived upgrade and refresh.

The authorisation code buys a one-hour token; that token is exchanged for a 60 day token, which
can be refreshed while it is still valid and at least a day old. There is no refresh token, so the
long-lived token itself is stored as the access token.
"""

from datetime import timedelta
from typing import Any

import httpx
from django.utils import timezone

from audit.actors import Actor
from connections import services
from connections.models import Connection, ConnectionStatus, CredentialKind
from connections.oauth import TokenSet
from connections.providers import OAuthConfig
from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)
from core.locks import LockNotAcquiredError, redis_lock
from social.instagram.client import HOST, PROVIDER

TOKEN_URL = "https://api.instagram.com/oauth/access_token"  # noqa: S105 - a public endpoint
TIMEOUT = 20.0
SYSTEM = Actor.system("instagram")
DEFAULT_LIFETIME_SECONDS = 60 * 24 * 3600
REFRESH_WITHIN = timedelta(days=14)
MIN_TOKEN_AGE = timedelta(days=1)
HTTP_SERVER_ERROR = 500
HTTP_CLIENT_ERROR = 400
HTTP_TOO_MANY = 429
AUTH_REJECTED = {400, 401, 403}


def _send(method: str, url: str, *, client: httpx.Client | None, **kwargs: Any) -> dict[str, Any]:
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        response = http.request(method, url, **kwargs)
    except httpx.TransportError as exc:
        raise RetryableProviderError("Instagram is unreachable", provider=PROVIDER) from exc
    finally:
        if client is None:
            http.close()
    if response.status_code == HTTP_TOO_MANY:
        raise RateLimitedError("token endpoint throttled", provider=PROVIDER)
    if response.status_code >= HTTP_SERVER_ERROR:
        raise RetryableProviderError("token endpoint unavailable", provider=PROVIDER)
    if response.status_code >= HTTP_CLIENT_ERROR:
        if response.status_code in AUTH_REJECTED:
            raise AuthExpiredError("authorisation was rejected", provider=PROVIDER)
        raise PermanentProviderError("token request rejected", provider=PROVIDER)
    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise PermanentProviderError(
            "token endpoint returned invalid JSON", provider=PROVIDER
        ) from exc
    return body


def _first_token(body: dict[str, Any]) -> dict[str, Any]:
    """The token payload is flat in older responses and wrapped in ``data`` in newer ones."""
    rows = body.get("data")
    return rows[0] if isinstance(rows, list) and rows else body


def exchange(
    config: OAuthConfig,
    code: str,
    redirect_uri: str,
    verifier: str,
    *,
    client: httpx.Client | None = None,
) -> TokenSet:
    """Authorisation code to a 60 day token."""
    short = _first_token(
        _send(
            "POST",
            TOKEN_URL,
            client=client,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code.removesuffix("#_"),
            },
        )
    )
    permissions = str(short.get("permissions", "")).replace(",", " ").split()
    long_lived = _send(
        "GET",
        f"{HOST}/access_token",
        client=client,
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": config.client_secret,
            "access_token": short["access_token"],
        },
    )
    expires_in = int(long_lived.get("expires_in") or DEFAULT_LIFETIME_SECONDS)
    return TokenSet(
        access_token=str(long_lived["access_token"]),
        refresh_token=None,
        expires_at=timezone.now() + timedelta(seconds=expires_in),
        scopes=permissions,
    )


def refresh(connection: Connection, *, client: httpx.Client | None = None) -> bool:
    """Extend the token's life by 60 days. Returns whether a new token was stored."""
    try:
        with redis_lock(f"refresh:{connection.pk}", ttl_seconds=60):
            token = services.read_secret(connection, CredentialKind.ACCESS_TOKEN)
            try:
                body = _send(
                    "GET",
                    f"{HOST}/refresh_access_token",
                    client=client,
                    params={"grant_type": "ig_refresh_token", "access_token": token},
                )
            except AuthExpiredError:
                services.set_status(connection, ConnectionStatus.EXPIRED, "authorisation expired")
                return False
            services.store_secret(
                connection, CredentialKind.ACCESS_TOKEN, str(body["access_token"]), actor=SYSTEM
            )
            expires_in = int(body.get("expires_in") or DEFAULT_LIFETIME_SECONDS)
            connection.expires_at = timezone.now() + timedelta(seconds=expires_in)
            connection.save(update_fields=["expires_at", "updated_at"])
            services.set_status(connection, ConnectionStatus.ACTIVE)
            return True
    except LockNotAcquiredError:
        return False


def refresh_due(*, client: httpx.Client | None = None) -> int:
    """Refresh every Instagram token that expires within two weeks (and is old enough)."""
    due = Connection.objects.filter(
        provider=PROVIDER,
        status__in=[ConnectionStatus.ACTIVE, ConnectionStatus.DEGRADED],
        expires_at__isnull=False,
        expires_at__lte=timezone.now() + REFRESH_WITHIN,
    )
    refreshed = 0
    for connection in due:
        try:
            refreshed += int(refresh(connection, client=client))
        except RetryableProviderError, PermanentProviderError:
            continue
    return refreshed
