"""Connection descriptor for OpenAI Ads: one API key per ad account."""

from ads.openai import actions
from ads.openai.client import PROVIDER, identify_account
from connections.models import Connection, ConnectionStatus
from connections.providers import (
    AuthType,
    FormField,
    HealthResult,
    ProviderDescriptor,
    register_provider,
)
from core.errors import AuthExpiredError, DomainError


def health_check(connection: Connection) -> HealthResult:
    try:
        with actions.client_factory(connection) as client:
            account = client.get_account()
    except AuthExpiredError:
        return HealthResult(ConnectionStatus.EXPIRED, "the API key was rejected")
    except DomainError as exc:
        return HealthResult(ConnectionStatus.DEGRADED, str(exc)[:200])
    review = (account.get("review") or {}).get("status", "")
    if account.get("status") != "active":
        return HealthResult(ConnectionStatus.DEGRADED, f"account status: {account.get('status')}")
    return HealthResult(ConnectionStatus.ACTIVE, f"review: {review}" if review else "")


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=PROVIDER,
            display_name="OpenAI Ads",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="api_key",
                    label="Ads API key",
                    secret=True,
                    help_text="Created in Ads Manager settings; scoped to one ad account.",
                ),
            ),
            identify=identify_account,
            health_check=health_check,
        )
    )
