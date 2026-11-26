"""Facebook Login for the Marketing API: code exchange, long-lived token and account discovery.

The authorisation code buys a short-lived user token that is exchanged at once for a long-lived one
(about 60 days). Facebook has no refresh token; asking for a new long-lived token with a still
valid one is the renewal, and when it does not extend the life the person signs in again.
"""

from datetime import timedelta
from typing import Any

import httpx
from django.conf import settings
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

PROVIDER = "meta"
TIMEOUT = 20.0
SYSTEM = Actor.system("meta")
DEFAULT_LIFETIME_SECONDS = 60 * 24 * 3600
REFRESH_WITHIN = timedelta(days=14)
HTTP_SERVER_ERROR = 500
HTTP_CLIENT_ERROR = 400
HTTP_TOO_MANY = 429
AUTH_REJECTED = {400, 401, 403}
AUTH_ERROR_CODES = {190, 102}


def graph_url() -> str:
    return f"https://graph.facebook.com/{settings.PROVIDER_API_VERSIONS['meta']}"


def dialog_url() -> str:
    return f"https://www.facebook.com/{settings.PROVIDER_API_VERSIONS['meta']}/dialog/oauth"


def send(path: str, *, client: httpx.Client | None = None, **params: Any) -> dict[str, Any]:
    """One GET against the Graph API with the platform's error taxonomy."""
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        response = http.get(f"{graph_url()}{path}", params=params)
    except httpx.TransportError as exc:
        raise RetryableProviderError("Meta is unreachable", provider=PROVIDER) from exc
    finally:
        if client is None:
            http.close()
    if response.status_code == HTTP_TOO_MANY:
        raise RateLimitedError("Meta throttled the request", provider=PROVIDER)
    if response.status_code >= HTTP_SERVER_ERROR:
        raise RetryableProviderError("Meta is unavailable", provider=PROVIDER)
    if response.status_code >= HTTP_CLIENT_ERROR:
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        code = error.get("code") if isinstance(error, dict) else None
        if code in AUTH_ERROR_CODES or response.status_code in AUTH_REJECTED:
            raise AuthExpiredError("authorisation was rejected", provider=PROVIDER)
        raise PermanentProviderError("Meta rejected the request", provider=PROVIDER)
    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise PermanentProviderError("Meta returned invalid JSON", provider=PROVIDER) from exc
    return body


def granted_permissions(token: str, *, client: httpx.Client | None = None) -> list[str]:
    body = send("/me/permissions", client=client, access_token=token)
    return [
        str(row["permission"])
        for row in body.get("data", [])
        if row.get("status") == "granted" and row.get("permission")
    ]


def long_lived(
    config: OAuthConfig, token: str, *, client: httpx.Client | None = None
) -> dict[str, Any]:
    return send(
        "/oauth/access_token",
        client=client,
        grant_type="fb_exchange_token",
        client_id=config.client_id,
        client_secret=config.client_secret,
        fb_exchange_token=token,
    )


def exchange(
    config: OAuthConfig,
    code: str,
    redirect_uri: str,
    verifier: str,
    *,
    client: httpx.Client | None = None,
) -> TokenSet:
    """Authorisation code to a long-lived user token."""
    del verifier  # Facebook Login does not use PKCE
    short = send(
        "/oauth/access_token",
        client=client,
        client_id=config.client_id,
        client_secret=config.client_secret,
        redirect_uri=redirect_uri,
        code=code,
    )
    upgraded = long_lived(config, str(short["access_token"]), client=client)
    token = str(upgraded["access_token"])
    expires_in = int(upgraded.get("expires_in") or DEFAULT_LIFETIME_SECONDS)
    return TokenSet(
        access_token=token,
        refresh_token=None,
        expires_at=timezone.now() + timedelta(seconds=expires_in),
        scopes=granted_permissions(token, client=client),
    )


def list_ad_accounts(token: str, *, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Every ad account the signed-in person can reach."""
    body = send(
        "/me/adaccounts",
        client=client,
        access_token=token,
        fields="id,account_id,name,account_status,currency,timezone_name",
        limit=100,
    )
    return list(body.get("data", []))


def refresh(
    connection: Connection, config: OAuthConfig, *, client: httpx.Client | None = None
) -> bool:
    """Ask for a new long-lived token. Returns whether the life was extended."""
    try:
        with redis_lock(f"refresh:{connection.pk}", ttl_seconds=60):
            token = services.read_secret(connection, CredentialKind.ACCESS_TOKEN)
            try:
                body = long_lived(config, token, client=client)
            except AuthExpiredError:
                services.set_status(connection, ConnectionStatus.EXPIRED, "authorisation expired")
                return False
            expires_at = timezone.now() + timedelta(
                seconds=int(body.get("expires_in") or DEFAULT_LIFETIME_SECONDS)
            )
            services.store_secret(
                connection, CredentialKind.ACCESS_TOKEN, str(body["access_token"]), actor=SYSTEM
            )
            extended = connection.expires_at is None or expires_at > connection.expires_at
            connection.expires_at = max(expires_at, connection.expires_at or expires_at)
            connection.save(update_fields=["expires_at", "updated_at"])
            return extended
    except LockNotAcquiredError:
        return False
