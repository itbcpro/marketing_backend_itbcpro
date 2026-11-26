"""Connection descriptors for Meta Ads: the shared app keys and Facebook Login."""

import uuid
from typing import Any

from ads.meta import auth, factory
from ads.meta.credentials import APP_PROVIDER, app_credentials
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

PROVIDER = auth.PROVIDER
SCOPES = ("ads_management", "ads_read")
ACCOUNT_OK = 1
STATUS_NAMES = {
    1: "active",
    2: "disabled",
    3: "unsettled",
    7: "pending_risk_review",
    8: "pending_settlement",
    9: "in_grace_period",
    100: "pending_closure",
    101: "closed",
    201: "any_active",
    202: "any_closed",
}


def oauth_config(workspace_id: uuid.UUID | None = None) -> OAuthConfig:
    keys = app_credentials(workspace_id)
    return OAuthConfig(
        authorize_url=auth.dialog_url(),
        token_url=f"{auth.graph_url()}/oauth/access_token",
        client_id=keys.app_id,
        client_secret=keys.app_secret,
        # Facebook wants the scopes comma separated and does not use PKCE.
        scopes=(",".join(SCOPES),),
        uses_pkce=False,
    )


def identify(access_token: str, workspace_id: uuid.UUID | None = None) -> tuple[str, str]:
    """Pick the first usable ad account; the owner can switch to another one afterwards."""
    del workspace_id
    accounts = auth.list_ad_accounts(access_token)
    if not accounts:
        raise AuthExpiredError(
            "this Facebook account has no ad account; create one in Meta Ads Manager first",
            provider=PROVIDER,
        )
    usable = [a for a in accounts if a.get("account_status") == ACCOUNT_OK] or accounts
    chosen = usable[0]
    return str(chosen["id"]), str(chosen.get("name") or chosen["id"])


def account_info(connection: Connection) -> dict[str, Any]:
    with factory.open_client(connection) as client:
        row = client.get_account()
    status = int(row.get("account_status") or 0)
    return {
        "id": connection.external_account_id,
        "name": row.get("name") or "",
        "currency_code": row.get("currency") or "",
        "timezone": row.get("timezone_name") or "UTC",
        "status": STATUS_NAMES.get(status, str(status)),
        "status_code": status,
        "disable_reason": row.get("disable_reason"),
        "spend_cap": row.get("spend_cap"),
        "amount_spent": row.get("amount_spent"),
    }


def health_check(connection: Connection) -> HealthResult:
    try:
        info = account_info(connection)
    except AuthExpiredError:
        return HealthResult(ConnectionStatus.EXPIRED, "authorisation expired")
    except DomainError as exc:
        return HealthResult(ConnectionStatus.DEGRADED, str(exc)[:200])
    if info["status_code"] != ACCOUNT_OK:
        return HealthResult(ConnectionStatus.DEGRADED, f"ad account status: {info['status']}")
    return HealthResult(ConnectionStatus.ACTIVE)


def refresh(connection: Connection) -> bool:
    return auth.refresh(connection, oauth_config(connection.workspace_id))


def identify_app_keys(_app_id: str) -> tuple[str, str]:
    return "meta-app", "Meta app keys"


def app_keys_health(connection: Connection) -> HealthResult:
    missing = app_credentials(connection.workspace_id).missing()
    if missing:
        return HealthResult(ConnectionStatus.DEGRADED, f"missing: {', '.join(missing)}")
    return HealthResult(ConnectionStatus.ACTIVE)


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=APP_PROVIDER,
            display_name="Meta app keys",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="app_id",
                    label="Meta app ID",
                    secret=True,
                    help_text="Meta developer console, App settings, Basic.",
                ),
                FormField(name="app_secret", label="Meta app secret", secret=True),
            ),
            credential_kinds={"app_id": "api_key", "app_secret": "client_secret"},
            identify=identify_app_keys,
            health_check=app_keys_health,
        )
    )
    register_provider(
        ProviderDescriptor(
            name=PROVIDER,
            display_name="Meta Ads",
            auth_type=AuthType.OAUTH,
            oauth=oauth_config,
            workspace_scoped=True,
            identify=identify,
            health_check=health_check,
            required_scopes=SCOPES,
            refresh=refresh,
        )
    )
