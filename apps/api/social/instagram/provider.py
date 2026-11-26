"""Connection descriptors for Instagram: Instagram Login OAuth plus the workspace's app keys."""

import uuid
from typing import Any

import httpx

from connections.models import Connection, ConnectionStatus
from connections.providers import (
    AuthType,
    FormField,
    HealthResult,
    OAuthConfig,
    ProviderDescriptor,
    register_provider,
)
from core.errors import AuthExpiredError, DomainError
from social.instagram import auth, factory
from social.instagram.client import ACCOUNT_FIELDS, HOST, PROVIDER, api_version
from social.instagram.credentials import APP_PROVIDER, app_credentials

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
SCOPES = (
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
    "instagram_business_manage_insights",
)
PROFESSIONAL_TYPES = {"BUSINESS", "MEDIA_CREATOR", "CREATOR"}


def oauth_config(workspace_id: uuid.UUID | None = None) -> OAuthConfig:
    keys = app_credentials(workspace_id)
    return OAuthConfig(
        authorize_url=AUTHORIZE_URL,
        token_url=auth.TOKEN_URL,
        client_id=keys.app_id,
        client_secret=keys.app_secret,
        # Instagram wants the scopes comma separated and does not support PKCE.
        scopes=(",".join(SCOPES),),
        uses_pkce=False,
        extra_authorize_params={"force_reauth": "true"},
    )


def identify(access_token: str, workspace_id: uuid.UUID | None = None) -> tuple[str, str]:
    """The professional account the token belongs to: ``(user id, display name)``."""
    del workspace_id
    with httpx.Client(base_url=f"{HOST}/{api_version()}", timeout=auth.TIMEOUT) as http:
        body = auth._send(
            "GET",
            "/me",
            client=http,
            params={"fields": "user_id,username"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    account_id = str(body.get("user_id") or body.get("id") or "")
    if not account_id:
        raise AuthExpiredError("Instagram did not return an account", provider=PROVIDER)
    username = str(body.get("username") or account_id)
    return account_id, f"@{username}"


def account_info(connection: Connection) -> dict[str, Any]:
    with factory.open_client(connection) as client:
        return client.me()


def health_check(connection: Connection) -> HealthResult:
    try:
        info = account_info(connection)
    except AuthExpiredError:
        return HealthResult(ConnectionStatus.EXPIRED, "authorisation expired")
    except DomainError as exc:
        return HealthResult(ConnectionStatus.DEGRADED, str(exc)[:200])
    if str(info.get("account_type", "")).upper() not in PROFESSIONAL_TYPES:
        return HealthResult(
            ConnectionStatus.DEGRADED, "this is not a professional (business or creator) account"
        )
    return HealthResult(ConnectionStatus.ACTIVE)


def identify_app_keys(_app_id: str) -> tuple[str, str]:
    return "instagram-app", "Instagram app keys"


def app_keys_health(connection: Connection) -> HealthResult:
    missing = app_credentials(connection.workspace_id).missing()
    if missing:
        return HealthResult(ConnectionStatus.DEGRADED, f"missing: {', '.join(missing)}")
    return HealthResult(ConnectionStatus.ACTIVE)


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=APP_PROVIDER,
            display_name="Instagram app keys",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="app_id",
                    label="Instagram app ID",
                    secret=True,
                    help_text="Meta developer console, Instagram API with Instagram Login.",
                ),
                FormField(name="app_secret", label="Instagram app secret", secret=True),
            ),
            credential_kinds={"app_id": "api_key", "app_secret": "client_secret"},
            identify=identify_app_keys,
            health_check=app_keys_health,
        )
    )
    register_provider(
        ProviderDescriptor(
            name=PROVIDER,
            display_name="Instagram",
            auth_type=AuthType.OAUTH,
            oauth=oauth_config,
            workspace_scoped=True,
            identify=identify,
            health_check=health_check,
            required_scopes=SCOPES,
            exchange=auth.exchange,
            refresh=auth.refresh,
        )
    )


__all__ = ["ACCOUNT_FIELDS", "SCOPES", "register"]
