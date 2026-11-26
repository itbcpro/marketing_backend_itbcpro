"""Connection descriptor for Claude: a workspace can bring its own Anthropic API key."""

import httpx

from connections.models import Connection, ConnectionStatus, CredentialKind
from connections.providers import (
    AuthType,
    FormField,
    HealthResult,
    ProviderDescriptor,
    register_provider,
)
from connections.services import read_secret
from core.errors import (
    AuthExpiredError,
    DomainError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)

PROVIDER = "anthropic"
MODELS_URL = "https://api.anthropic.com/v1/models"
API_VERSION = "2023-06-01"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
UNAUTHORIZED = {401, 403}
MAX_ERROR_CHARS = 300
WORKSPACE_PREFIX = "wrkspc_"
WORKSPACE_HEADER = "anthropic-workspace-id"


def workspace_of(connection: Connection) -> str:
    """The Anthropic workspace id stored as the connection's account id, if it has one."""
    return (
        connection.external_account_id
        if connection.external_account_id.startswith(WORKSPACE_PREFIX)
        else ""
    )


def _probe(
    api_key: str, workspace_id: str = "", *, transport: httpx.BaseTransport | None = None
) -> None:
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION}
    if workspace_id:
        headers[WORKSPACE_HEADER] = workspace_id
    with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
        response = client.get(
            MODELS_URL,
            params={"limit": 1},
            headers=headers,
        )
    if response.status_code in UNAUTHORIZED:
        raise AuthExpiredError("the API key was rejected", provider=PROVIDER)
    if response.status_code == httpx.codes.OK:
        return
    detail = f"HTTP {response.status_code}: {response.text[:MAX_ERROR_CHARS]}"
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        raise RateLimitedError(detail, provider=PROVIDER)
    if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise RetryableProviderError(detail, provider=PROVIDER)
    raise PermanentProviderError(detail, provider=PROVIDER)


def identify(api_key: str, workspace_id: str = "") -> tuple[str, str]:
    _probe(api_key, workspace_id)
    return workspace_id or "claude-api", "Claude API"


def health_check(connection: Connection) -> HealthResult:
    try:
        _probe(read_secret(connection, CredentialKind.API_KEY), workspace_of(connection))
    except AuthExpiredError:
        return HealthResult(ConnectionStatus.EXPIRED, "the API key was rejected")
    except DomainError as exc:
        return HealthResult(ConnectionStatus.DEGRADED, str(exc)[:200])
    return HealthResult(ConnectionStatus.ACTIVE)


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=PROVIDER,
            display_name="Claude (Anthropic)",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="api_key",
                    label="Anthropic API key",
                    secret=True,
                    help_text="Used by this workspace's agents. Falls back to the platform key.",
                ),
                FormField(
                    name="workspace_id",
                    label="Anthropic workspace ID (optional)",
                    required=False,
                    pattern=r"wrkspc_[A-Za-z0-9]+",
                    help_text="Only for keys not scoped to a workspace. Starts with wrkspc_.",
                ),
            ),
            identify=identify,
            health_check=health_check,
        )
    )
