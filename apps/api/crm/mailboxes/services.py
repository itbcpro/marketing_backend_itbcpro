"""Connected mailboxes: connect, list, disconnect, pick the sender, and read replies back."""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.common.normalize import InvalidValueError, normalise_email
from crm.consent.models import Channel
from crm.inbox.models import ChannelAccount, Direction, Message, MessageStatus
from crm.mailboxes import oauth, providers, smtp
from crm.mailboxes.models import AuthRequest, Mailbox, MailboxStatus, Provider
from crm.mailboxes.oauth import Grant
from crm.mailboxes.sealing import seal_credentials, wipe_credentials
from crm.sending import services as sending
from workspaces.models import Membership
from workspaces.permissions import Permission, role_allows

ACCOUNT_PROVIDER = "mailbox"
NAME_LIMIT = 200
SYNC_LOCK_STATES = (MailboxStatus.CONNECTED,)
MANAGE = Permission.CRM_MANAGE_CHANNELS


def _default_limit(provider: str) -> int:
    return int(settings.CRM_MAILBOX_DEFAULT_LIMITS.get(provider, 0)) or 1


def can_manage_all(membership: Membership) -> bool:
    return role_allows(membership.role, MANAGE)


# ---- reading ------------------------------------------------------------------------------


def visible(membership: Membership) -> list[Mailbox]:
    """Everything for managers; otherwise the person's own and the shared mailboxes."""
    rows = Mailbox.objects.for_workspace(membership.workspace_id)
    if not can_manage_all(membership):
        rows = rows.filter(Q(owner_id=membership.user_id) | Q(owner__isnull=True))
    return list(rows.order_by("email"))


def get(workspace_id: uuid.UUID, mailbox_id: uuid.UUID) -> Mailbox:
    found = Mailbox.objects.for_workspace(workspace_id).filter(pk=mailbox_id).first()
    if found is None:
        raise NotFoundError("mailbox not found")
    return found


def editable_by(membership: Membership, mailbox: Mailbox) -> Mailbox:
    """The mailbox if this person may change it (their own, or any for a manager)."""
    if mailbox.owner_id == membership.user_id or can_manage_all(membership):
        return mailbox
    raise NotFoundError("mailbox not found")


def sent_today(mailbox: Mailbox) -> int:
    if mailbox.account_id is None:
        return 0
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        Message.objects.for_workspace(mailbox.workspace_id)
        .filter(
            channel_account_id=mailbox.account_id,
            direction=Direction.OUTBOUND,
            created_at__gte=start,
        )
        .exclude(status=MessageStatus.BLOCKED)
        .count()
    )


# ---- connecting ---------------------------------------------------------------------------


def _clean_email(value: str) -> str:
    try:
        return normalise_email(value)
    except InvalidValueError as exc:
        raise ValidationFailedError("invalid email", errors={"email": "invalid_email"}) from exc


def _account(mailbox: Mailbox) -> ChannelAccount:
    account, _ = ChannelAccount.objects.update_or_create(
        workspace_id=mailbox.workspace_id,
        channel=Channel.EMAIL,
        external_id=mailbox.email,
        defaults={
            "provider": ACCOUNT_PROVIDER,
            "display_name": mailbox.display_name,
            "status": "active",
            "config": {"mailbox_id": str(mailbox.pk)},
        },
    )
    return account


def _save_new(mailbox: Mailbox, actor: Actor) -> Mailbox:
    try:
        with transaction.atomic():
            existing = Mailbox.objects.filter(
                workspace_id=mailbox.workspace_id, email=mailbox.email
            ).first()
            if existing is not None and existing.status != MailboxStatus.DISABLED:
                raise ConflictError("mailbox exists", errors={"email": "mailbox_exists"})
            if existing is not None:  # reconnecting a disconnected mailbox reuses its row
                mailbox.pk = existing.pk
                mailbox._state.adding = False
                mailbox.created_at = existing.created_at
            conflict = ChannelAccount.objects.filter(
                workspace_id=mailbox.workspace_id, channel=Channel.EMAIL, external_id=mailbox.email
            ).exclude(provider=ACCOUNT_PROVIDER)
            if conflict.exists():
                raise ConflictError("address in use", errors={"email": "address_in_use"})
            mailbox.save()
            mailbox.account = _account(mailbox)
            mailbox.save(update_fields=["account", "updated_at"])
            record_event(
                action="crm.mailbox.connected",
                actor=actor,
                workspace_id=mailbox.workspace_id,
                target_type="crm_mailbox",
                target_id=mailbox.pk,
                after={"email": mailbox.email, "provider": mailbox.provider},
            )
    except IntegrityError as exc:
        raise ConflictError("mailbox exists", errors={"email": "mailbox_exists"}) from exc
    return mailbox


def connect_smtp(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    owner_id: uuid.UUID | None,
    email: str,
    display_name: str,
    server: dict[str, Any],
    password: str,
    daily_limit: int | None = None,
) -> Mailbox:
    """Add an SMTP mailbox after proving the login works. Nothing is sent."""
    address = _clean_email(email)
    settings_ = smtp.clean_server(server)
    if not password:
        raise ValidationFailedError("password required", errors={"password": "required"})
    try:
        smtp.test_login(settings_, password)
    except smtp.SmtpError as exc:
        raise ValidationFailedError("smtp login failed", errors={"smtp": exc.code}) from exc
    mailbox = Mailbox(
        workspace_id=workspace_id,
        owner_id=owner_id,
        provider=Provider.SMTP,
        email=address,
        display_name=display_name.strip()[:NAME_LIMIT],
        daily_limit=daily_limit or _default_limit(Provider.SMTP),
        config={
            "host": settings_.host,
            "port": settings_.port,
            "security": settings_.security,
            "username": settings_.username,
        },
    )
    seal_credentials(mailbox, {"password": password})
    return _save_new(mailbox, actor)


def connect_oauth(*, grant: Grant, actor: Actor) -> Mailbox:
    """Turn a finished sign-in into a mailbox."""
    mailbox = Mailbox(
        workspace_id=grant.workspace_id,
        owner_id=None if grant.shared else grant.user_id,
        provider=grant.provider,
        email=_clean_email(grant.email),
        display_name=grant.display_name.strip()[:NAME_LIMIT],
        daily_limit=_default_limit(grant.provider),
        config={"scopes": grant.scopes},
        sync_enabled=grant.sync,
    )
    seal_credentials(mailbox, grant.credentials)
    return _save_new(mailbox, actor)


def update(
    *,
    workspace_id: uuid.UUID,
    mailbox: Mailbox,
    display_name: str | None = None,
    daily_limit: int | None = None,
    sync_enabled: bool | None = None,
) -> Mailbox:
    if display_name is not None:
        mailbox.display_name = display_name.strip()[:NAME_LIMIT]
    if daily_limit is not None:
        if daily_limit < 1:
            raise ValidationFailedError("invalid limit", errors={"daily_limit": "invalid_value"})
        mailbox.daily_limit = daily_limit
    if sync_enabled is not None:
        wanted = sync_enabled and mailbox.provider != Provider.SMTP
        needed = oauth.spec_for(mailbox.provider).sync_scopes if wanted else ()
        if wanted and not set(needed) <= set(mailbox.config.get("scopes", [])):
            raise ValidationFailedError("reconnect needed", errors={"sync_enabled": "reconnect"})
        mailbox.sync_enabled = wanted
    mailbox.save()
    if mailbox.account_id:
        ChannelAccount.objects.filter(pk=mailbox.account_id).update(
            display_name=mailbox.display_name
        )
    return mailbox


def disconnect(*, workspace_id: uuid.UUID, actor: Actor, mailbox: Mailbox) -> Mailbox:
    """Stop using the mailbox and forget its credentials. Sent messages stay in the history."""
    oauth.revoke(mailbox)
    wipe_credentials(mailbox)
    mailbox.status = MailboxStatus.DISABLED
    mailbox.sync_enabled = False
    mailbox.last_error = ""
    mailbox.save()
    if mailbox.account_id:
        ChannelAccount.objects.filter(pk=mailbox.account_id).update(status="inactive")
    record_event(
        action="crm.mailbox.disconnected",
        actor=actor,
        workspace_id=workspace_id,
        target_type="crm_mailbox",
        target_id=mailbox.pk,
    )
    return mailbox


# ---- choosing and using a sender ----------------------------------------------------------


def block_reason(workspace_id: uuid.UUID, account_id: uuid.UUID) -> str | None:
    """Why a send from this inbox sender must not happen, or None when it may (or is not ours).

    Returns the gateway's block code for a mailbox that is off, broken or over its daily limit.
    """
    mailbox = Mailbox.objects.for_workspace(workspace_id).filter(account_id=account_id).first()
    if mailbox is None:
        return None
    if mailbox.status != MailboxStatus.CONNECTED:
        return "mailbox_unavailable"
    return "mailbox_limit" if sent_today(mailbox) >= mailbox.daily_limit else ""


def sender_for(workspace_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID | None:
    """The inbox sender to use when a person writes by hand: their own connected mailbox.

    None means "use the workspace's default sender". A mailbox that is over its daily limit is still
    returned, so the gateway blocks the send with a clear reason instead of quietly changing who
    the message comes from.
    """
    row = (
        Mailbox.objects.for_workspace(workspace_id)
        .filter(owner_id=user_id, status=MailboxStatus.CONNECTED, account__isnull=False)
        .order_by("created_at")
        .first()
    )
    return row.account_id if row else None


def mailbox_of(workspace_id: uuid.UUID, account: ChannelAccount) -> Mailbox:
    mailbox = Mailbox.objects.for_workspace(workspace_id).filter(account_id=account.pk).first()
    if mailbox is None:
        raise NotFoundError("mailbox not found")
    return mailbox


def mark_sent(mailbox: Mailbox) -> None:
    Mailbox.objects.filter(pk=mailbox.pk).update(last_sent_at=timezone.now(), last_error="")


def mark_failed(mailbox: Mailbox, failure: providers.MailboxSendError) -> None:
    fields: dict[str, Any] = {"last_error": failure.code}
    if failure.needs_attention:
        fields["status"] = MailboxStatus.NEEDS_ATTENTION
    Mailbox.objects.filter(pk=mailbox.pk).update(**fields)


# ---- reading replies ----------------------------------------------------------------------


def sync(*, workspace_id: uuid.UUID, mailbox_id: uuid.UUID) -> int:
    """Pull new inbox mail of one mailbox into the CRM. Returns how many messages were read."""
    mailbox = get(workspace_id, mailbox_id)
    if not mailbox.sync_enabled or mailbox.status != MailboxStatus.CONNECTED:
        return 0
    count = 0
    cursor = mailbox.sync_cursor
    try:
        for raw, moment in providers.fetch_new(mailbox):
            # Mail we cannot place (no valid sender) is skipped, not retried forever.
            with contextlib.suppress(ValidationFailedError):
                sending.ingest_email(workspace_id=workspace_id, raw=raw)
            cursor, count = moment, count + 1
    except providers.MailboxSendError as failure:
        mark_failed(mailbox, failure)
        if not failure.retryable:
            return count
    finally:
        if cursor != mailbox.sync_cursor:
            mailbox.sync_cursor = cursor
        mailbox.last_synced_at = timezone.now()
        mailbox.save(update_fields=["sync_cursor", "last_synced_at", "updated_at"])
    return count


def syncable() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(workspace, mailbox) pairs for the periodic reader."""
    rows = Mailbox.objects.filter(sync_enabled=True, status=MailboxStatus.CONNECTED)
    return list(rows.values_list("workspace_id", "pk"))


def purge_expired_requests() -> int:
    """Drop sign-in requests nobody finished."""
    deleted, _ = AuthRequest.objects.filter(expires_at__lt=timezone.now()).delete()
    return int(deleted)
