"""Google Ads setup screens: what is configured, which accounts can be reached, choosing one."""

import uuid
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from ninja import Router, Schema

from ads.google import factory
from ads.google.client import PROVIDER, customer_id
from ads.google.credentials import app_credentials
from ads.google.provider import customer_info
from connections import lifecycle
from connections.models import Connection, ConnectionStatus
from core.errors import DomainError, NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["ads-google"], auth=api_auth)

CLIENT_QUERY = (
    "SELECT customer_client.id, customer_client.descriptive_name, customer_client.currency_code, "
    "customer_client.time_zone, customer_client.manager, customer_client.status, "
    "customer_client.level FROM customer_client WHERE customer_client.status = 'ENABLED'"
)


class StatusOut(Schema):
    configured: bool
    missing: list[str]
    redirect_uri: str


class AccountOut(Schema):
    id: str
    name: str
    currency: str
    time_zone: str
    manager: bool
    login_customer_id: str


class AccountsOut(Schema):
    selected: str
    login_customer_id: str
    accounts: list[AccountOut]


class SelectIn(Schema):
    connection_id: uuid.UUID
    customer_id: str
    login_customer_id: str = ""


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
        raise NotFoundError("Google Ads connection not found")
    return found


@router.get("/status", response=StatusOut, operation_id="google_ads_status")
def setup_status(request: HttpRequest) -> StatusOut:
    """Whether the platform has the credentials Google requires (set by the administrator)."""
    membership = resolve_workspace(request, Permission.VIEW)
    missing = app_credentials(membership.workspace_id).missing()
    return StatusOut(
        configured=not missing,
        missing=missing,
        redirect_uri=f"{settings.WEB_BASE_URL}/bff/api/v1/connections/oauth/{PROVIDER}/callback",
    )


@router.get("/accounts", response=AccountsOut, operation_id="google_ads_accounts")
def accounts(request: HttpRequest, connection_id: uuid.UUID) -> AccountsOut:
    """Every account this Google user can reach, including client accounts under a manager."""
    connection = _connection(request, connection_id, Permission.MANAGE_CONNECTIONS)
    found: dict[str, AccountOut] = {}
    with factory.open_client(connection) as client:
        for top in client.list_accessible():
            client.login_customer = top
            try:
                rows = client.search(CLIENT_QUERY, cid=top)
            except DomainError:
                found.setdefault(
                    top,
                    AccountOut(
                        id=top,
                        name="",
                        currency="",
                        time_zone="",
                        manager=False,
                        login_customer_id="",
                    ),
                )
                continue
            for row in rows:
                item: dict[str, Any] = row.get("customerClient", {})
                cid = customer_id(str(item.get("id", "")))
                if not cid:
                    continue
                if cid in found and found[cid].login_customer_id and cid == top:
                    continue  # already reached through its manager; keep that route
                found[cid] = AccountOut(
                    id=cid,
                    name=str(item.get("descriptiveName") or ""),
                    currency=str(item.get("currencyCode") or ""),
                    time_zone=str(item.get("timeZone") or ""),
                    manager=bool(item.get("manager")),
                    login_customer_id="" if cid == top else top,
                )
    ordered = sorted(found.values(), key=lambda a: (a.manager, a.name.lower()))
    return AccountsOut(
        selected=connection.external_account_id,
        login_customer_id=str(connection.config.get("login_customer_id", "")),
        accounts=ordered,
    )


@router.post("/accounts/select", response=AccountsOut, operation_id="google_ads_select_account")
def select_account(request: HttpRequest, body: SelectIn) -> AccountsOut:
    """Point the connection at one client account (and the manager it is reached through)."""
    connection = _connection(request, body.connection_id, Permission.MANAGE_CONNECTIONS)
    chosen = customer_id(body.customer_id)
    login = customer_id(body.login_customer_id)
    if not chosen or len(chosen) != 10:  # noqa: PLR2004 - Google customer ids have 10 digits
        raise ValidationFailedError("a customer id has 10 digits")
    clash = (
        Connection.objects.filter(
            workspace_id=connection.workspace_id, provider=PROVIDER, external_account_id=chosen
        )
        .exclude(pk=connection.pk)
        .exclude(status=ConnectionStatus.REVOKED)
        .exists()
    )
    if clash:
        raise ValidationFailedError("that account is already connected")
    connection.external_account_id = chosen
    connection.config = {**connection.config, "login_customer_id": login}
    try:
        info = customer_info(connection)
        connection.display_name = (info["name"] or f"Google Ads {chosen}")[:200]
    except DomainError:
        connection.display_name = f"Google Ads {chosen}"
    connection.save(update_fields=["external_account_id", "config", "display_name", "updated_at"])
    lifecycle.check_connection(connection)
    return AccountsOut(selected=chosen, login_customer_id=login, accounts=[])
