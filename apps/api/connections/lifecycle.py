"""Connection lifecycle: token refresh (single flight), health checks and revocation."""

from datetime import timedelta

import httpx
from django.db.models import Q
from django.utils import timezone

from audit.actors import Actor
from connections import oauth, services
from connections.models import Connection, ConnectionStatus, Credential, CredentialKind
from connections.providers import (
    AuthType,
    HealthResult,
    UnknownProviderError,
    get_provider,
    missing_scopes,
    oauth_config_for,
)
from core.errors import AuthExpiredError, DomainError, NotFoundError
from core.locks import LockNotAcquiredError, redis_lock
from core.logging import get_logger

log = get_logger(__name__)

REFRESH_WINDOW = timedelta(minutes=15)
EXPIRY_WARNING_WINDOW = timedelta(days=7)
SYSTEM = Actor.system("connections")


def refresh_connection(connection: Connection, *, client: httpx.Client | None = None) -> bool:
    """Refresh the access token once across all workers. Returns whether a refresh happened."""
    descriptor = get_provider(connection.provider)
    if descriptor.oauth is None:
        return False
    if descriptor.refresh is not None:
        return descriptor.refresh(connection)
    try:
        with redis_lock(f"refresh:{connection.pk}", ttl_seconds=60):
            refresh_token = services.read_secret(connection, CredentialKind.REFRESH_TOKEN)
            tokens = oauth.refresh(
                oauth_config_for(descriptor, connection.workspace_id),
                provider=connection.provider,
                refresh_token=refresh_token,
                client=client,
            )
            services.store_secret(
                connection, CredentialKind.ACCESS_TOKEN, tokens.access_token, actor=SYSTEM
            )
            if tokens.refresh_token:
                services.store_secret(
                    connection, CredentialKind.REFRESH_TOKEN, tokens.refresh_token, actor=SYSTEM
                )
            connection.expires_at = tokens.expires_at
            if tokens.scopes:
                connection.granted_scopes = tokens.scopes
            connection.save(update_fields=["expires_at", "granted_scopes", "updated_at"])
            services.set_status(connection, ConnectionStatus.ACTIVE)
            return True
    except LockNotAcquiredError:
        return False
    except AuthExpiredError:
        services.set_status(connection, ConnectionStatus.EXPIRED, "authorisation expired")
        return False


def refresh_due_connections(*, client: httpx.Client | None = None) -> int:
    """Refresh every OAuth connection whose token expires within the window."""
    cutoff = timezone.now() + REFRESH_WINDOW
    due = Connection.objects.filter(
        Q(expires_at__isnull=False, expires_at__lte=cutoff),
        status__in=[ConnectionStatus.ACTIVE, ConnectionStatus.DEGRADED],
        credentials__kind=CredentialKind.REFRESH_TOKEN,
    ).distinct()
    refreshed = 0
    for connection in due:
        try:
            refreshed += int(refresh_connection(connection, client=client))
        except DomainError as exc:
            log.warning("token_refresh_failed", connection=str(connection.pk), code=exc.code.value)
        except UnknownProviderError:
            log.warning("token_refresh_unknown_provider", provider=connection.provider)
    return refreshed


def check_connection(connection: Connection) -> HealthResult:
    """Run the provider's health check plus generic expiry and scope checks, then store it."""
    result = HealthResult(ConnectionStatus.ACTIVE)
    try:
        descriptor = get_provider(connection.provider)
    except UnknownProviderError:
        result = HealthResult(ConnectionStatus.ERROR, "provider is not registered")
    else:
        if descriptor.health_check is not None:
            try:
                result = descriptor.health_check(connection)
            except AuthExpiredError:
                result = HealthResult(ConnectionStatus.EXPIRED, "authorisation expired")
            except DomainError as exc:
                result = HealthResult(ConnectionStatus.DEGRADED, exc.code.value)
        gaps = missing_scopes(descriptor, connection.granted_scopes)
        if result.status == ConnectionStatus.ACTIVE and gaps:
            result = HealthResult(ConnectionStatus.DEGRADED, f"missing scopes: {', '.join(gaps)}")
    now = timezone.now()
    expiring = connection.expires_at and connection.expires_at <= now + EXPIRY_WARNING_WINDOW
    has_refresh = Credential.objects.filter(
        connection=connection, kind=CredentialKind.REFRESH_TOKEN
    ).exists()
    if result.status == ConnectionStatus.ACTIVE and expiring and not has_refresh:
        expired = connection.expires_at is not None and connection.expires_at <= now
        result = HealthResult(
            ConnectionStatus.EXPIRED if expired else ConnectionStatus.DEGRADED,
            "token expired" if expired else "token expires soon",
        )
    services.set_status(connection, result.status, result.detail)
    return result


def check_all_connections() -> dict[str, int]:
    counts: dict[str, int] = {}
    live = Connection.objects.exclude(status=ConnectionStatus.REVOKED)
    for connection in live:
        try:
            outcome = check_connection(connection).status.value
        except Exception:
            log.exception("connection_health_check_crashed", connection=str(connection.pk))
            outcome = "crashed"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def revoke_connection(
    connection: Connection, *, actor: Actor, client: httpx.Client | None = None
) -> None:
    """Best-effort provider revoke, then destroy local credentials and mark it revoked."""
    try:
        descriptor = get_provider(connection.provider)
        if descriptor.auth_type == AuthType.OAUTH and descriptor.oauth is not None:
            for kind in (CredentialKind.REFRESH_TOKEN, CredentialKind.ACCESS_TOKEN):
                try:
                    secret = services.read_secret(connection, kind)
                    oauth.revoke(
                        oauth_config_for(descriptor, connection.workspace_id),
                        provider=connection.provider,
                        token=secret,
                        client=client,
                    )
                    break
                except NotFoundError, DomainError:
                    continue
    except UnknownProviderError:
        pass
    services.destroy_credentials(connection, actor=actor)
    services.set_status(connection, ConnectionStatus.REVOKED, "revoked by user")
