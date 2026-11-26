"""Meta Ads screens: configuration, reachable ad accounts and assets, choosing an account."""

import uuid
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from ninja import Router, Schema

from ads.meta import auth, factory
from ads.meta.credentials import app_credentials
from ads.meta.provider import PROVIDER, SCOPES, account_info
from connections import lifecycle, services
from connections.models import Connection, ConnectionStatus, CredentialKind
from core.errors import DomainError, NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["ads-meta"], auth=api_auth)


class MetaAdsStatusOut(Schema):
    configured: bool
    missing: list[str]
    redirect_uri: str
    scopes: list[str]


class MetaAccountOut(Schema):
    id: str
    name: str
    currency: str
    time_zone: str
    status: str


class MetaAccountsOut(Schema):
    selected: str
    accounts: list[MetaAccountOut]


class MetaSelectIn(Schema):
    connection_id: uuid.UUID
    account_id: str


class MetaPageOut(Schema):
    id: str
    name: str


class MetaPixelOut(Schema):
    id: str
    name: str


class MetaInstagramOut(Schema):
    id: str
    username: str


class MetaAssetsOut(Schema):
    pages: list[MetaPageOut]
    instagram_accounts: list[MetaInstagramOut]
    pixels: list[MetaPixelOut]


def _connection(
    request: HttpRequest, connection_id: uuid.UUID, permission: Permission
) -> Connection:
    membership = resolve_workspace(request, permission)
    found = (
        Connection.objects.filter(
            pk=connection_id, workspace_id=membership.workspace_id, provider=PROVIDER
        )
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )
    if found is None:
        raise NotFoundError("Meta Ads connection not found")
    return found


def _act(account_id: str) -> str:
    digits = account_id.removeprefix("act_").strip()
    if not digits.isdigit():
        raise ValidationFailedError("an ad account id looks like act_1234567890")
    return f"act_{digits}"


@router.get("/status", response=MetaAdsStatusOut, operation_id="meta_ads_status")
def setup_status(request: HttpRequest) -> MetaAdsStatusOut:
    """Whether the platform has the Meta app keys (set once on the Meta setup screen)."""
    membership = resolve_workspace(request, Permission.VIEW)
    missing = app_credentials(membership.workspace_id).missing()
    return MetaAdsStatusOut(
        configured=not missing,
        missing=missing,
        redirect_uri=f"{settings.WEB_BASE_URL}/bff/api/v1/connections/oauth/{PROVIDER}/callback",
        scopes=list(SCOPES),
    )


@router.get("/accounts", response=MetaAccountsOut, operation_id="meta_ads_accounts")
def accounts(request: HttpRequest, connection_id: uuid.UUID) -> MetaAccountsOut:
    """Every ad account this Facebook user can reach."""
    connection = _connection(request, connection_id, Permission.MANAGE_CONNECTIONS)
    token = services.read_secret(connection, CredentialKind.ACCESS_TOKEN)
    rows = auth.list_ad_accounts(token)
    return MetaAccountsOut(
        selected=connection.external_account_id,
        accounts=[
            MetaAccountOut(
                id=str(r["id"]),
                name=str(r.get("name") or r["id"]),
                currency=str(r.get("currency") or ""),
                time_zone=str(r.get("timezone_name") or ""),
                status=str(r.get("account_status") or ""),
            )
            for r in sorted(rows, key=lambda r: str(r.get("name") or "").lower())
        ],
    )


@router.post("/accounts/select", response=MetaAccountsOut, operation_id="meta_ads_select_account")
def select_account(request: HttpRequest, body: MetaSelectIn) -> MetaAccountsOut:
    """Point the connection at one ad account."""
    connection = _connection(request, body.connection_id, Permission.MANAGE_CONNECTIONS)
    chosen = _act(body.account_id)
    clash = (
        Connection.objects.filter(
            workspace_id=connection.workspace_id, provider=PROVIDER, external_account_id=chosen
        )
        .exclude(pk=connection.pk)
        .exclude(status=ConnectionStatus.REVOKED)
        .exists()
    )
    if clash:
        raise ValidationFailedError("that ad account is already connected")
    connection.external_account_id = chosen
    try:
        info = account_info(connection)
        connection.display_name = (info["name"] or chosen)[:200]
    except DomainError:
        connection.display_name = chosen
    connection.save(update_fields=["external_account_id", "display_name", "updated_at"])
    lifecycle.check_connection(connection)
    return MetaAccountsOut(selected=chosen, accounts=[])


@router.get("/assets", response=MetaAssetsOut, operation_id="meta_ads_assets")
def assets(request: HttpRequest, connection_id: uuid.UUID) -> MetaAssetsOut:
    """The pages, Instagram accounts and pixels an ad in this account can use."""
    connection = _connection(request, connection_id, Permission.VIEW)

    def read(edge: str, fields: str) -> list[dict[str, Any]]:
        try:
            return client.list_edge(connection.external_account_id, edge, fields)
        except DomainError:
            return []  # an edge the token cannot read is simply empty

    with factory.open_client(connection) as client:
        pages = read("promote_pages", "id,name")
        instagram = read("instagram_accounts", "id,username")
        pixels = read("adspixels", "id,name")
    return MetaAssetsOut(
        pages=[MetaPageOut(id=str(p["id"]), name=str(p.get("name") or p["id"])) for p in pages],
        instagram_accounts=[
            MetaInstagramOut(id=str(i["id"]), username=str(i.get("username") or i["id"]))
            for i in instagram
        ],
        pixels=[MetaPixelOut(id=str(p["id"]), name=str(p.get("name") or p["id"])) for p in pixels],
    )
