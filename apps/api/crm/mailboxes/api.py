"""Manage connected mailboxes. Nothing here ever returns a password or a token."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.errors import PermissionDeniedError, ValidationFailedError
from crm.mailboxes import oauth, services, smtp
from crm.mailboxes.models import Mailbox, MailboxStatus, Provider
from crm.mailboxes.sealing import open_credentials
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.models import Membership
from workspaces.permissions import Permission

router = Router(tags=["crm-mailboxes"], auth=api_auth)


class MailboxOut(Schema):
    id: uuid.UUID
    provider: str
    email: str
    display_name: str
    status: str
    last_error: str
    owner_id: uuid.UUID | None
    mine: bool
    shared: bool
    daily_limit: int
    sent_today: int
    sync_enabled: bool
    can_sync: bool
    last_synced_at: datetime | None
    last_sent_at: datetime | None
    host: str


class ProvidersOut(Schema):
    oauth: list[str]
    smtp_ports: list[int]
    smtp_securities: list[str]


class SmtpIn(Schema):
    email: str
    display_name: str = ""
    host: str
    port: int
    security: str
    username: str
    password: str
    shared: bool = False
    daily_limit: int | None = None


class StartIn(Schema):
    provider: str
    sync: bool = False
    shared: bool = False


class StartOut(Schema):
    url: str


class PatchIn(Schema):
    display_name: str | None = None
    daily_limit: int | None = None
    sync_enabled: bool | None = None


class TestOut(Schema):
    ok: bool
    code: str


def _out(membership: Membership, row: Mailbox) -> MailboxOut:
    scopes = set(row.config.get("scopes", []))
    can_sync = (
        row.provider != Provider.SMTP and set(oauth.spec_for(row.provider).sync_scopes) <= scopes
    )
    return MailboxOut(
        id=row.pk,
        provider=row.provider,
        email=row.email,
        display_name=row.display_name,
        status=row.status,
        last_error=row.last_error,
        owner_id=row.owner_id,
        mine=row.owner_id == membership.user_id,
        shared=row.owner_id is None,
        daily_limit=row.daily_limit,
        sent_today=services.sent_today(row),
        sync_enabled=row.sync_enabled,
        can_sync=can_sync,
        last_synced_at=row.last_synced_at,
        last_sent_at=row.last_sent_at,
        host=str(row.config.get("host", "")),
    )


def _actor(membership: Membership) -> Actor:
    return Actor.human(membership.user_id)


def _owner_for(membership: Membership, shared: bool) -> uuid.UUID | None:
    """A shared mailbox belongs to the workspace and needs the channel-management permission."""
    if not shared:
        return membership.user_id
    if not services.can_manage_all(membership):
        raise PermissionDeniedError()
    return None


@router.get("/email/mailboxes", response=list[MailboxOut], operation_id="crm_mailboxes_list")
def list_mailboxes(request: HttpRequest) -> list[MailboxOut]:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    return [_out(membership, row) for row in services.visible(membership)]


@router.get(
    "/email/mailboxes/providers", response=ProvidersOut, operation_id="crm_mailboxes_providers"
)
def providers_available(request: HttpRequest) -> ProvidersOut:
    resolve_workspace(request, Permission.CRM_SEND)
    return ProvidersOut(
        oauth=oauth.available(),
        smtp_ports=list(smtp.PORTS),
        smtp_securities=list(smtp.SECURITIES),
    )


@router.post(
    "/email/mailboxes/smtp", response={201: MailboxOut}, operation_id="crm_mailboxes_connect_smtp"
)
def connect_smtp(request: HttpRequest, payload: SmtpIn) -> Status[MailboxOut]:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    owner = _owner_for(membership, payload.shared)
    row = services.connect_smtp(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        owner_id=owner,
        email=payload.email,
        display_name=payload.display_name,
        server={
            "host": payload.host,
            "port": payload.port,
            "security": payload.security,
            "username": payload.username,
        },
        password=payload.password,
        daily_limit=payload.daily_limit,
    )
    return Status(201, _out(membership, row))


@router.post(
    "/email/mailboxes/oauth/start", response=StartOut, operation_id="crm_mailboxes_oauth_start"
)
def oauth_start(request: HttpRequest, payload: StartIn) -> StartOut:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    _owner_for(membership, payload.shared)
    try:
        url = oauth.start(
            workspace_id=membership.workspace_id,
            user_id=membership.user_id,
            provider=payload.provider,
            sync=payload.sync,
            shared=payload.shared,
        )
    except oauth.OAuthError as exc:
        raise ValidationFailedError("cannot connect", errors={"provider": exc.code}) from exc
    return StartOut(url=url)


def _editable(request: HttpRequest, mailbox_id: uuid.UUID) -> tuple[Membership, Mailbox]:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    row = services.get(membership.workspace_id, mailbox_id)
    return membership, services.editable_by(membership, row)


@router.patch(
    "/email/mailboxes/{mailbox_id}", response=MailboxOut, operation_id="crm_mailboxes_update"
)
def update_mailbox(request: HttpRequest, mailbox_id: uuid.UUID, payload: PatchIn) -> MailboxOut:
    membership, row = _editable(request, mailbox_id)
    changed = services.update(
        workspace_id=membership.workspace_id, mailbox=row, **payload.model_dump(exclude_unset=True)
    )
    return _out(membership, changed)


@router.post(
    "/email/mailboxes/{mailbox_id}/disconnect",
    response=MailboxOut,
    operation_id="crm_mailboxes_disconnect",
)
def disconnect_mailbox(request: HttpRequest, mailbox_id: uuid.UUID) -> MailboxOut:
    membership, row = _editable(request, mailbox_id)
    changed = services.disconnect(
        workspace_id=membership.workspace_id, actor=_actor(membership), mailbox=row
    )
    return _out(membership, changed)


@router.post(
    "/email/mailboxes/{mailbox_id}/test", response=TestOut, operation_id="crm_mailboxes_test"
)
def test_mailbox(request: HttpRequest, mailbox_id: uuid.UUID) -> TestOut:
    """Check the connection works now: an SMTP login, or a token refresh for OAuth mailboxes."""
    _, row = _editable(request, mailbox_id)
    if row.status == MailboxStatus.DISABLED:
        return TestOut(ok=False, code="disconnected")
    try:
        if row.provider == Provider.SMTP:
            config = row.config
            server = smtp.Server(
                str(config.get("host", "")),
                int(config.get("port", 0)),
                str(config.get("security", "")),
                str(config.get("username", "")),
            )
            smtp.test_login(server, str(open_credentials(row).get("password", "")))
        else:
            oauth.access_token(row)
    except (smtp.SmtpError, oauth.OAuthError) as exc:
        return TestOut(ok=False, code=exc.code)
    row.status, row.last_error = MailboxStatus.CONNECTED, ""
    row.save(update_fields=["status", "last_error", "updated_at"])
    return TestOut(ok=True, code="")
