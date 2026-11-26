"""Connection descriptor for a WordPress site: address, user name and an application password."""

from typing import Any

from connections.models import Connection, ConnectionStatus
from connections.providers import (
    AuthType,
    FormField,
    HealthResult,
    ProviderDescriptor,
    register_provider,
)
from content.wordpress import factory
from content.wordpress.client import PROVIDER, WordPressClient, normalize_site_url
from core.errors import AuthExpiredError, DomainError, PermanentProviderError, ValidationFailedError

EDITOR_CAPABILITIES = ("edit_posts", "edit_others_posts", "publish_posts")


def identify(application_password: str, site_url: str = "", username: str = "") -> tuple[str, str]:
    """Verify the credentials against the site and return ``(site origin, site name)``."""
    origin = normalize_site_url(site_url)
    client = factory.build(origin, username=username, password=application_password)
    try:
        root = client.root()
        client.me()
    except AuthExpiredError as exc:
        raise ValidationFailedError(
            "WordPress rejected the user name or application password"
        ) from exc
    except PermanentProviderError as exc:
        if str(exc) == "rest_not_found":
            raise ValidationFailedError("the WordPress REST API is not reachable here") from exc
        raise ValidationFailedError(str(exc)) from exc
    return origin, str(root.get("name") or origin)


def health_check(connection: Connection) -> HealthResult:
    client = factory.for_connection(connection)
    try:
        client.root()
        me = client.me()
    except AuthExpiredError:
        return HealthResult(ConnectionStatus.EXPIRED, "the application password was rejected")
    except DomainError as exc:
        return HealthResult(ConnectionStatus.DEGRADED, str(exc)[:200])
    capabilities: dict[str, Any] = me.get("capabilities") or {}
    missing = [c for c in EDITOR_CAPABILITIES if not capabilities.get(c)]
    if missing:
        return HealthResult(
            ConnectionStatus.DEGRADED,
            "this user can read but not publish; use an Editor or Administrator",
        )
    return HealthResult(ConnectionStatus.ACTIVE)


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=PROVIDER,
            display_name="WordPress",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="site_url",
                    label="Site address",
                    pattern=r"(https://)?[A-Za-z0-9.-]+(:\d+)?/?",
                    help_text="The https address of the site, for example https://example.com",
                ),
                FormField(
                    name="username",
                    label="WordPress user name",
                    help_text="The user the application password was created for.",
                ),
                FormField(
                    name="application_password",
                    label="Application password",
                    secret=True,
                    help_text="WordPress admin, Users, Profile, Application Passwords.",
                ),
            ),
            credential_kinds={"application_password": "access_token"},
            identify=identify,
            health_check=health_check,
        )
    )


__all__ = ["WordPressClient", "register"]
