"""Connection descriptor for Google Ads: Google OAuth plus the platform's developer token."""

import uuid
from typing import Any

from ads.google import client as google_client
from ads.google import factory
from ads.google.client import PROVIDER
from ads.google.credentials import APP_PROVIDER, app_credentials
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

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a public endpoint, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPE = "https://www.googleapis.com/auth/adwords"
CUSTOMER_QUERY = (
    "SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, "
    "customer.status, customer.manager, customer.test_account FROM customer LIMIT 1"
)
ACCOUNT_OK_STATUS = "ENABLED"


def oauth_config(workspace_id: uuid.UUID | None = None) -> OAuthConfig:
    keys = app_credentials(workspace_id)
    return OAuthConfig(
        authorize_url=AUTHORIZE_URL,
        token_url=TOKEN_URL,
        client_id=keys.client_id,
        client_secret=keys.client_secret,
        scopes=(SCOPE,),
        revoke_url=REVOKE_URL,
        uses_pkce=True,
        extra_authorize_params={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
    )


def identify(access_token: str, workspace_id: uuid.UUID | None = None) -> tuple[str, str]:
    """Pick the first reachable account; the owner can switch to another one afterwards."""
    accounts = google_client.list_accessible_customers(access_token, workspace_id=workspace_id)
    if not accounts:
        raise AuthExpiredError("this Google account has no Google Ads access", provider=PROVIDER)
    return accounts[0], f"Google Ads {accounts[0]}"


def customer_info(connection: Connection) -> dict[str, Any]:
    with factory.open_client(connection) as client:
        rows = client.search(CUSTOMER_QUERY)
    row = (rows[0] if rows else {}).get("customer", {})
    return {
        "id": row.get("id"),
        "name": row.get("descriptiveName") or "",
        "currency_code": row.get("currencyCode") or "",
        "timezone": row.get("timeZone") or "UTC",
        "status": str(row.get("status", "")).lower(),
        "manager": bool(row.get("manager")),
        "test_account": bool(row.get("testAccount")),
    }


def health_check(connection: Connection) -> HealthResult:
    try:
        info = customer_info(connection)
    except AuthExpiredError:
        return HealthResult(ConnectionStatus.EXPIRED, "authorisation expired")
    except DomainError as exc:
        return HealthResult(ConnectionStatus.DEGRADED, str(exc)[:200])
    if info["status"].upper() != ACCOUNT_OK_STATUS:
        return HealthResult(ConnectionStatus.DEGRADED, f"account status: {info['status']}")
    if info["manager"]:
        return HealthResult(
            ConnectionStatus.DEGRADED, "this is a manager account; choose a client account"
        )
    return HealthResult(ConnectionStatus.ACTIVE)


def identify_app_keys(_client_id: str) -> tuple[str, str]:
    return "google-ads-app", "Google Ads app keys"


def app_keys_health(connection: Connection) -> HealthResult:
    missing = app_credentials(connection.workspace_id).missing()
    if missing:
        return HealthResult(ConnectionStatus.DEGRADED, f"missing: {', '.join(missing)}")
    return HealthResult(ConnectionStatus.ACTIVE)


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=APP_PROVIDER,
            display_name="Google Ads app keys",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="client_id",
                    label="Google OAuth client ID",
                    secret=True,
                    help_text="Google Cloud console, OAuth client with the Google Ads API enabled.",
                ),
                FormField(name="client_secret", label="Google OAuth client secret", secret=True),
                FormField(
                    name="developer_token",
                    label="Google Ads developer token (optional)",
                    secret=True,
                    required=False,
                    help_text="Google no longer requires one; only needed for older projects.",
                ),
            ),
            credential_kinds={
                "client_id": "api_key",
                "client_secret": "client_secret",
                "developer_token": "developer_token",
            },
            identify=identify_app_keys,
            health_check=app_keys_health,
        )
    )
    register_provider(
        ProviderDescriptor(
            name=PROVIDER,
            display_name="Google Ads",
            auth_type=AuthType.OAUTH,
            oauth=oauth_config,
            workspace_scoped=True,
            identify=identify,
            health_check=health_check,
            required_scopes=(SCOPE,),
        )
    )
