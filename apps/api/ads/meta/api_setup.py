"""The shared Meta setup screen: one place for the app keys that Instagram and Meta Ads both need.

A Meta app has two sets of keys: the app itself (Facebook Login and the Marketing API, used for ads
and pages) and the Instagram product inside it (Instagram Login). The screen saves both and shows
what each product still needs. The keys are stored as ordinary connections, so they are encrypted,
audited and shared with the rest of the platform.
"""

import uuid

from django.conf import settings
from django.http import HttpRequest
from ninja import Router, Schema

from ads.meta import credentials as meta_credentials
from ads.meta.provider import PROVIDER as ADS_PROVIDER
from ads.meta.provider import SCOPES as ADS_SCOPES
from audit.actors import Actor
from connections import lifecycle, services
from connections.models import Connection, ConnectionStatus, CredentialKind
from core.errors import ValidationFailedError
from identity.authentication import api_auth
from social.instagram import credentials as instagram_credentials
from social.instagram.client import PROVIDER as INSTAGRAM_PROVIDER
from social.instagram.provider import SCOPES as INSTAGRAM_SCOPES
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["meta"], auth=api_auth)

MAX_KEY_CHARS = 200


class MetaProductOut(Schema):
    configured: bool
    missing: list[str]
    redirect_uri: str
    scopes: list[str]
    connected: int


class MetaSetupStatusOut(Schema):
    facebook: MetaProductOut
    instagram: MetaProductOut
    webhook_url: str
    webhook_verify_token_set: bool
    public_url_ready: bool


class MetaKeysIn(Schema):
    app_id: str = ""
    app_secret: str = ""
    instagram_app_id: str = ""
    instagram_app_secret: str = ""


def _redirect(provider: str) -> str:
    return f"{settings.WEB_BASE_URL}/bff/api/v1/connections/oauth/{provider}/callback"


def _count(workspace_id: uuid.UUID, provider: str) -> int:
    return (
        Connection.objects.filter(workspace_id=workspace_id, provider=provider)
        .exclude(status=ConnectionStatus.REVOKED)
        .count()
    )


@router.get("/status", response=MetaSetupStatusOut, operation_id="meta_status")
def status(request: HttpRequest) -> MetaSetupStatusOut:
    """What each Meta product still needs before its Connect button works."""
    membership = resolve_workspace(request, Permission.VIEW)
    workspace_id = membership.workspace_id
    base = str(settings.PUBLIC_BASE_URL).rstrip("/")
    facebook_missing = meta_credentials.app_credentials(workspace_id).missing()
    instagram_missing = instagram_credentials.app_credentials(workspace_id).missing()
    return MetaSetupStatusOut(
        facebook=MetaProductOut(
            configured=not facebook_missing,
            missing=facebook_missing,
            redirect_uri=_redirect(ADS_PROVIDER),
            scopes=list(ADS_SCOPES),
            connected=_count(workspace_id, ADS_PROVIDER),
        ),
        instagram=MetaProductOut(
            configured=not instagram_missing,
            missing=instagram_missing,
            redirect_uri=_redirect(INSTAGRAM_PROVIDER),
            scopes=list(INSTAGRAM_SCOPES),
            connected=_count(workspace_id, INSTAGRAM_PROVIDER),
        ),
        webhook_url=f"{base}/bff/api/v1/social/instagram/webhook",
        webhook_verify_token_set=bool(settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN),
        public_url_ready=base.startswith("https://"),
    )


def _save_pair(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    provider: str,
    external_id: str,
    display: str,
    app_id: str,
    app_secret: str,
) -> None:
    membership = resolve_workspace(request, Permission.MANAGE_CONNECTIONS)
    actor = Actor.human(membership.user_id)
    connection = (
        Connection.objects.filter(workspace_id=workspace_id, provider=provider)
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    ) or services.create_connection(
        workspace_id=membership.workspace_id,
        provider=provider,
        external_account_id=external_id,
        display_name=display,
        actor=actor,
    )
    services.store_secret(connection, CredentialKind.API_KEY, app_id, actor=actor)
    services.store_secret(connection, CredentialKind.CLIENT_SECRET, app_secret, actor=actor)
    lifecycle.check_connection(connection)


@router.post("/keys", response=MetaSetupStatusOut, operation_id="meta_save_keys")
def save_keys(request: HttpRequest, body: MetaKeysIn) -> MetaSetupStatusOut:
    """Save the app keys (either pair or both). A pair needs both of its values."""
    membership = resolve_workspace(request, Permission.MANAGE_CONNECTIONS)
    pairs = (
        (
            meta_credentials.APP_PROVIDER,
            "meta-app",
            "Meta app keys",
            body.app_id.strip(),
            body.app_secret.strip(),
        ),
        (
            instagram_credentials.APP_PROVIDER,
            "instagram-app",
            "Instagram app keys",
            body.instagram_app_id.strip(),
            body.instagram_app_secret.strip(),
        ),
    )
    wanted = [p for p in pairs if p[3] or p[4]]
    if not wanted:
        raise ValidationFailedError("enter an app ID and its secret")
    for provider, external_id, display, app_id, app_secret in wanted:
        if not app_id or not app_secret:
            raise ValidationFailedError("each app ID needs its secret, and each secret its ID")
        if max(len(app_id), len(app_secret)) > MAX_KEY_CHARS:
            raise ValidationFailedError("a key is too long")
        _save_pair(
            request, membership.workspace_id, provider, external_id, display, app_id, app_secret
        )
    return status(request)
