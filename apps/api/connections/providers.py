"""Provider registry: descriptors for connection forms, OAuth configuration and health checks.

Each platform adapter registers a ``ProviderDescriptor`` at startup. The API and UI are driven by
these descriptors, so adding a platform never touches connection code (open/closed).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from connections.models import Connection, ConnectionStatus


class AuthType(StrEnum):
    OAUTH = "oauth"
    API_KEY = "api_key"
    TOKEN = "token"  # noqa: S105 - a category name, not a secret


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
    revoke_url: str = ""
    uses_pkce: bool = True
    extra_authorize_params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FormField:
    """One input of the connection form. ``label`` and ``help_text`` are translatable strings."""

    name: str
    label: str
    secret: bool = False
    required: bool = True
    pattern: str = ""
    help_text: str = ""


@dataclass(frozen=True, slots=True)
class HealthResult:
    status: ConnectionStatus
    detail: str = ""


HealthCheck = Callable[[Connection], HealthResult]
ScopeReader = Callable[[Connection], list[str]]


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    name: str
    display_name: str
    auth_type: AuthType
    fields: tuple[FormField, ...] = ()
    oauth: Callable[..., OAuthConfig] | None = None
    workspace_scoped: bool = False
    """When set, ``oauth`` and ``identify`` also receive ``workspace_id``."""
    identify: Callable[..., tuple[str, str]] | None = None
    """Given an access token, return ``(external_account_id, display_name)`` after OAuth."""
    health_check: HealthCheck | None = None
    required_scopes: tuple[str, ...] = ()
    credential_kinds: dict[str, str] = field(default_factory=dict)
    """Maps form field name to the credential kind it is stored as (default: ``api_key``)."""
    exchange: Callable[..., Any] | None = None
    """Replaces the standard code-for-token exchange: ``(config, code, redirect_uri, verifier)``
    returning a ``TokenSet`` (platforms with a second long-lived token step)."""
    refresh: Callable[[Connection], bool] | None = None
    """Replaces the refresh-token grant for platforms that refresh the access token itself."""


def oauth_config_for(descriptor: ProviderDescriptor, workspace_id: Any) -> OAuthConfig:
    assert descriptor.oauth is not None  # noqa: S101 - callers check the provider uses OAuth
    return descriptor.oauth(workspace_id) if descriptor.workspace_scoped else descriptor.oauth()


_registry: dict[str, ProviderDescriptor] = {}


class UnknownProviderError(KeyError):
    """Raised when no descriptor is registered for a provider name."""


def register_provider(descriptor: ProviderDescriptor) -> None:
    _registry[descriptor.name] = descriptor


def unregister_provider(name: str) -> None:
    _registry.pop(name, None)


def get_provider(name: str) -> ProviderDescriptor:
    try:
        return _registry[name]
    except KeyError as exc:
        raise UnknownProviderError(name) from exc


def all_providers() -> list[ProviderDescriptor]:
    return sorted(_registry.values(), key=lambda d: d.name)


def missing_scopes(descriptor: ProviderDescriptor, granted: list[str]) -> list[str]:
    """Scopes the feature set needs that the user did not grant (drives the permission-gap UI)."""
    return [scope for scope in descriptor.required_scopes if scope not in set(granted)]


def descriptor_as_dict(descriptor: ProviderDescriptor) -> dict[str, Any]:
    return {
        "name": descriptor.name,
        "display_name": str(descriptor.display_name),
        "auth_type": descriptor.auth_type.value,
        "required_scopes": list(descriptor.required_scopes),
        "fields": [
            {
                "name": f.name,
                "label": str(f.label),
                "secret": f.secret,
                "required": f.required,
                "pattern": f.pattern,
                "help_text": str(f.help_text),
            }
            for f in descriptor.fields
        ],
    }
