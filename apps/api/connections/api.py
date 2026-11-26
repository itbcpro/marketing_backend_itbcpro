"""Connection management endpoints. Secrets are write-only: no endpoint ever returns them."""

import re
import uuid
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from ninja import Router, Schema, Status

from audit.actors import Actor
from connections import lifecycle, oauth, services
from connections.models import Connection, CredentialKind
from connections.providers import (
    AuthType,
    UnknownProviderError,
    all_providers,
    descriptor_as_dict,
    get_provider,
    missing_scopes,
    oauth_config_for,
)
from core.api.auth import session_auth
from core.errors import ErrorCode, NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission
from workspaces.services import require_permission

router = Router(tags=["connections"], auth=api_auth)
OAUTH_STATE_TTL = 600


class ConnectionOut(Schema):
    id: uuid.UUID
    provider: str
    display_name: str
    external_account_id: str
    status: str
    health_detail: str
    last_health_at: datetime | None
    expires_at: datetime | None
    api_version: str
    granted_scopes: list[str]
    missing_scopes: list[str]


class ConnectionCreateIn(Schema):
    provider: str
    external_account_id: str = ""
    display_name: str = ""
    fields: dict[str, str]


class OAuthStartOut(Schema):
    authorization_url: str


def _out(connection: Connection) -> ConnectionOut:
    try:
        gaps = missing_scopes(get_provider(connection.provider), connection.granted_scopes)
    except UnknownProviderError:
        gaps = []
    return ConnectionOut(
        id=connection.pk,
        provider=connection.provider,
        display_name=connection.display_name,
        external_account_id=connection.external_account_id,
        status=connection.status,
        health_detail=connection.health_detail,
        last_health_at=connection.last_health_at,
        expires_at=connection.expires_at,
        api_version=connection.api_version,
        granted_scopes=connection.granted_scopes,
        missing_scopes=gaps,
    )


def _get(request: HttpRequest, connection_id: uuid.UUID, permission: Permission) -> Connection:
    membership = resolve_workspace(request, permission)
    connection = Connection.objects.filter(
        pk=connection_id, workspace_id=membership.workspace_id
    ).first()
    if connection is None:
        raise NotFoundError("connection not found")
    return connection


def _descriptor(name: str) -> Any:
    try:
        return get_provider(name)
    except UnknownProviderError as exc:
        raise NotFoundError("unknown provider") from exc


@router.get("/providers", response=list[dict[str, Any]], operation_id="connections_providers")
def list_providers(request: HttpRequest) -> list[dict[str, Any]]:
    resolve_workspace(request, Permission.VIEW)
    return [descriptor_as_dict(d) for d in all_providers()]


@router.get("/", response=list[ConnectionOut], operation_id="connections_list")
def list_connections(request: HttpRequest) -> list[ConnectionOut]:
    membership = resolve_workspace(request, Permission.VIEW)
    rows = Connection.objects.filter(workspace_id=membership.workspace_id).order_by(
        "provider", "display_name"
    )
    return [_out(c) for c in rows]


def _identify(descriptor: Any, fields: dict[str, str], display_name: str) -> tuple[str, str]:
    """Ask the platform which account a key belongs to, so users never type account ids."""
    if descriptor.identify is None:
        raise ValidationFailedError("external_account_id is required for this provider")
    secret_name = next((f.name for f in descriptor.fields if f.secret), "")
    extras = {
        f.name: fields[f.name] for f in descriptor.fields if not f.secret and fields.get(f.name)
    }
    external_id, discovered = descriptor.identify(fields.get(secret_name, ""), **extras)
    return external_id, display_name or discovered


@router.post("/", response=ConnectionOut, operation_id="connections_create")
def create_connection(request: HttpRequest, payload: ConnectionCreateIn) -> ConnectionOut:
    membership = resolve_workspace(request, Permission.MANAGE_CONNECTIONS)
    descriptor = _descriptor(payload.provider)
    if descriptor.auth_type == AuthType.OAUTH:
        raise ValidationFailedError("use the OAuth flow for this provider")
    # Pasted keys often carry a trailing newline or spaces, which providers reject as invalid.
    payload.fields = {name: value.strip() for name, value in payload.fields.items()}
    for field in descriptor.fields:
        value = payload.fields.get(field.name, "")
        if (field.required and not value) or (
            value and field.pattern and not re.fullmatch(field.pattern, value)
        ):
            raise ValidationFailedError(
                f"invalid value for {field.name}", code=ErrorCode.VALIDATION_FAILED
            )
    external_id, display_name = payload.external_account_id, payload.display_name
    if not external_id:
        external_id, display_name = _identify(descriptor, payload.fields, display_name)
    actor = Actor.human(membership.user_id)
    connection = services.create_connection(
        workspace_id=membership.workspace_id,
        provider=payload.provider,
        external_account_id=external_id,
        display_name=display_name,
        actor=actor,
    )
    for field in descriptor.fields:
        secret_value = payload.fields.get(field.name)
        if secret_value and field.secret:
            kind = CredentialKind(
                descriptor.credential_kinds.get(field.name, CredentialKind.API_KEY)
            )
            services.store_secret(connection, kind, secret_value, actor=actor)
    plain = {
        f.name: payload.fields[f.name]
        for f in descriptor.fields
        if not f.secret and payload.fields.get(f.name)
    }
    if plain:
        # Non-secret form values (site address, user name) stay readable for the adapter.
        connection.config = {**connection.config, "form": plain}
        connection.save(update_fields=["config", "updated_at"])
    lifecycle.check_connection(connection)
    connection.refresh_from_db()
    return _out(connection)


@router.post("/{connection_id}/test", response=ConnectionOut, operation_id="connections_test")
def test_connection(request: HttpRequest, connection_id: uuid.UUID) -> ConnectionOut:
    connection = _get(request, connection_id, Permission.MANAGE_CONNECTIONS)
    lifecycle.check_connection(connection)
    connection.refresh_from_db()
    return _out(connection)


@router.post("/{connection_id}/refresh", response=ConnectionOut, operation_id="connections_refresh")
def refresh_connection(request: HttpRequest, connection_id: uuid.UUID) -> ConnectionOut:
    connection = _get(request, connection_id, Permission.MANAGE_CONNECTIONS)
    lifecycle.refresh_connection(connection)
    connection.refresh_from_db()
    return _out(connection)


@router.delete("/{connection_id}", response={204: None}, operation_id="connections_revoke")
def revoke_connection(request: HttpRequest, connection_id: uuid.UUID) -> Status[None]:
    connection = _get(request, connection_id, Permission.MANAGE_CONNECTIONS)
    user: User = request.auth  # type: ignore[attr-defined]
    lifecycle.revoke_connection(connection, actor=Actor.human(user.pk))
    return Status(204, None)


# ---- OAuth ------------------------------------------------------------------------------------


# Providers that need one more step (choosing an account) go to their own page after signing in.
POST_CONNECT_PATH = {
    "google_ads": "/ads/google",
    "google_search": "/website",
    "instagram": "/social/instagram",
    "meta": "/ads/meta",
}


def _redirect_uri(provider: str) -> str:
    return f"{settings.WEB_BASE_URL}/bff/api/v1/connections/oauth/{provider}/callback"


@router.post(
    "/oauth/{provider}/start", response=OAuthStartOut, operation_id="connections_oauth_start"
)
def oauth_start(request: HttpRequest, provider: str) -> OAuthStartOut:
    membership = resolve_workspace(request, Permission.MANAGE_CONNECTIONS)
    descriptor = _descriptor(provider)
    if descriptor.auth_type != AuthType.OAUTH or descriptor.oauth is None:
        raise ValidationFailedError("provider does not use OAuth")
    authorization = oauth.build_authorization_request(
        oauth_config_for(descriptor, membership.workspace_id), redirect_uri=_redirect_uri(provider)
    )
    cache.set(
        f"oauth-state:{authorization.state}",
        {
            "provider": provider,
            "workspace_id": str(membership.workspace_id),
            "user_id": str(membership.user_id),
            "verifier": authorization.code_verifier,
        },
        OAUTH_STATE_TTL,
    )
    return OAuthStartOut(authorization_url=authorization.url)


@router.get(
    "/oauth/{provider}/callback", auth=session_auth, operation_id="connections_oauth_callback"
)
def oauth_callback(request: HttpRequest, provider: str, code: str, state: str) -> HttpResponse:
    pending = cache.get(f"oauth-state:{state}")
    cache.delete(f"oauth-state:{state}")  # a state value is usable exactly once
    user: User = request.auth  # type: ignore[attr-defined]
    if not pending or pending["provider"] != provider or pending["user_id"] != str(user.pk):
        raise ValidationFailedError(code=ErrorCode.INVALID_TOKEN)
    workspace_id = uuid.UUID(pending["workspace_id"])
    require_permission(user, workspace_id, Permission.MANAGE_CONNECTIONS)
    descriptor = _descriptor(provider)
    assert descriptor.oauth is not None  # noqa: S101 - guarded by the state written in oauth_start
    config = oauth_config_for(descriptor, workspace_id)
    if descriptor.exchange is not None:
        tokens = descriptor.exchange(config, code, _redirect_uri(provider), pending["verifier"])
    else:
        tokens = oauth.exchange_code(
            config,
            provider=provider,
            code=code,
            redirect_uri=_redirect_uri(provider),
            code_verifier=pending["verifier"],
        )
    if descriptor.identify is None:
        external_id, display = provider, provider
    elif descriptor.workspace_scoped:
        external_id, display = descriptor.identify(tokens.access_token, workspace_id=workspace_id)
    else:
        external_id, display = descriptor.identify(tokens.access_token)
    actor = Actor.human(user.pk)
    connection, _ = Connection.objects.get_or_create(
        workspace_id=workspace_id,
        provider=provider,
        external_account_id=external_id,
        defaults={"display_name": display},
    )
    services.store_secret(connection, CredentialKind.ACCESS_TOKEN, tokens.access_token, actor=actor)
    if tokens.refresh_token:
        services.store_secret(
            connection, CredentialKind.REFRESH_TOKEN, tokens.refresh_token, actor=actor
        )
    connection.expires_at = tokens.expires_at
    connection.granted_scopes = tokens.scopes
    connection.save(update_fields=["expires_at", "granted_scopes", "updated_at"])
    lifecycle.check_connection(connection)
    landing = POST_CONNECT_PATH.get(provider, "/connections")
    return HttpResponseRedirect(f"{settings.WEB_BASE_URL}{landing}?connected={provider}")
