"""Connection descriptors for Google Search Console: the OAuth app keys and the Google sign-in."""

import uuid

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
from content.google import gsc
from content.google.credentials import APP_PROVIDER, app_credentials
from core.errors import AuthExpiredError, DomainError

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a public endpoint, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SEARCH_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
SCOPES = ("openid", "email", SEARCH_SCOPE)
REQUIRED_SCOPES = (SEARCH_SCOPE,)


def oauth_config(workspace_id: uuid.UUID | None = None) -> OAuthConfig:
    keys = app_credentials(workspace_id)
    return OAuthConfig(
        authorize_url=AUTHORIZE_URL,
        token_url=TOKEN_URL,
        client_id=keys.client_id,
        client_secret=keys.client_secret,
        scopes=SCOPES,
        revoke_url=REVOKE_URL,
        uses_pkce=True,
        extra_authorize_params={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
    )


def identify(access_token: str, workspace_id: uuid.UUID | None = None) -> tuple[str, str]:
    """The Google account that signed in: ``(email, display name)``."""
    del workspace_id
    try:
        with httpx.Client(timeout=gsc.TIMEOUT, transport=gsc.transport) as client:
            response = client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.TransportError as exc:
        raise AuthExpiredError("could not reach Google", provider=gsc.PROVIDER) from exc
    if response.status_code != 200:  # noqa: PLR2004
        raise AuthExpiredError("Google did not accept the sign-in", provider=gsc.PROVIDER)
    email = str(response.json().get("email", ""))
    if not email:
        raise AuthExpiredError("Google did not return an email address", provider=gsc.PROVIDER)
    return email, f"Google ({email})"


def health_check(connection: Connection) -> HealthResult:
    try:
        sites = gsc.SearchConsoleClient(connection).list_sites()
    except AuthExpiredError:
        return HealthResult(ConnectionStatus.EXPIRED, "authorisation expired")
    except DomainError as exc:
        return HealthResult(ConnectionStatus.DEGRADED, str(exc)[:200])
    if not sites:
        return HealthResult(
            ConnectionStatus.DEGRADED, "this Google account has no Search Console property"
        )
    return HealthResult(ConnectionStatus.ACTIVE)


def identify_app_keys(_client_id: str) -> tuple[str, str]:
    return "google-search-app", "Google sign-in app keys"


def app_keys_health(connection: Connection) -> HealthResult:
    missing = app_credentials(connection.workspace_id).missing()
    if missing:
        return HealthResult(ConnectionStatus.DEGRADED, f"missing: {', '.join(missing)}")
    return HealthResult(ConnectionStatus.ACTIVE)


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=APP_PROVIDER,
            display_name="Google sign-in app keys (Search Console)",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="client_id",
                    label="Google OAuth client ID",
                    secret=True,
                    help_text="Google Cloud console, OAuth client of type Web application.",
                ),
                FormField(name="client_secret", label="Google OAuth client secret", secret=True),
            ),
            credential_kinds={"client_id": "api_key", "client_secret": "client_secret"},
            identify=identify_app_keys,
            health_check=app_keys_health,
        )
    )
    register_provider(
        ProviderDescriptor(
            name=gsc.PROVIDER,
            display_name="Google Search Console",
            auth_type=AuthType.OAUTH,
            oauth=oauth_config,
            workspace_scoped=True,
            identify=identify,
            health_check=health_check,
            required_scopes=REQUIRED_SCOPES,
        )
    )
