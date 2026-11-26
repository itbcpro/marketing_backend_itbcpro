"""Generic OAuth 2.0 authorization-code client with PKCE, refresh and revoke."""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from django.utils import timezone

from connections.providers import OAuthConfig
from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)

HTTP_TIMEOUT_SECONDS = 15.0
HTTP_TOO_MANY = 429
HTTP_SERVER_ERROR = 500
HTTP_CLIENT_ERROR = 400


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    url: str
    state: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def build_authorization_request(
    config: OAuthConfig, *, redirect_uri: str, scopes: tuple[str, ...] | None = None
) -> AuthorizationRequest:
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes if scopes is not None else config.scopes),
        "state": state,
        **config.extra_authorize_params,
    }
    if config.uses_pkce:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    return AuthorizationRequest(
        url=f"{config.authorize_url}?{urlencode(params)}", state=state, code_verifier=verifier
    )


def _token_set(payload: dict[str, Any]) -> TokenSet:
    expires_in = payload.get("expires_in")
    return TokenSet(
        access_token=str(payload["access_token"]),
        refresh_token=payload.get("refresh_token"),
        expires_at=timezone.now() + timedelta(seconds=int(expires_in)) if expires_in else None,
        scopes=str(payload.get("scope", "")).replace(",", " ").split(),
    )


def _raise_for(response: httpx.Response, provider: str) -> None:
    if response.status_code == HTTP_TOO_MANY:
        raise RateLimitedError("token endpoint throttled", provider=provider)
    if response.status_code >= HTTP_SERVER_ERROR:
        raise RetryableProviderError("token endpoint unavailable", provider=provider)
    if response.status_code >= HTTP_CLIENT_ERROR:
        try:
            error = response.json().get("error", "")
        except ValueError:
            error = ""
        if error in {"invalid_grant", "invalid_token", "unauthorized_client"}:
            raise AuthExpiredError(error, provider=provider)
        raise PermanentProviderError(error or "token request rejected", provider=provider)


def _post(
    url: str, data: dict[str, str], provider: str, client: httpx.Client | None
) -> dict[str, Any]:
    owned = client is None
    http = client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    try:
        response = http.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.TransportError as exc:
        raise RetryableProviderError("token endpoint unreachable", provider=provider) from exc
    finally:
        if owned:
            http.close()
    _raise_for(response, provider)
    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise PermanentProviderError(
            "token endpoint returned invalid JSON", provider=provider
        ) from exc
    return body


def exchange_code(
    config: OAuthConfig,
    *,
    provider: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client: httpx.Client | None = None,
) -> TokenSet:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }
    if config.uses_pkce:
        data["code_verifier"] = code_verifier
    return _token_set(_post(config.token_url, data, provider, client))


def refresh(
    config: OAuthConfig, *, provider: str, refresh_token: str, client: httpx.Client | None = None
) -> TokenSet:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }
    # Providers may omit the refresh token on refresh; the caller keeps the old one then.
    return _token_set(_post(config.token_url, data, provider, client))


def revoke(
    config: OAuthConfig, *, provider: str, token: str, client: httpx.Client | None = None
) -> None:
    if not config.revoke_url:
        return
    data = {"token": token, "client_id": config.client_id, "client_secret": config.client_secret}
    _post(config.revoke_url, data, provider, client)
