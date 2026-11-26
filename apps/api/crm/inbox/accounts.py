"""Sender accounts of a workspace (numbers, mailboxes, pages): the public read and write API."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import IntegrityError, transaction

from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.inbox.models import ChannelAccount, Message

ACTIVE = "active"


def list_accounts(workspace_id: uuid.UUID, channel: str) -> list[ChannelAccount]:
    rows = ChannelAccount.objects.for_workspace(workspace_id).filter(channel=channel)
    return list(rows.order_by("created_at"))


def any_account(workspace_id: uuid.UUID) -> bool:
    """Whether the workspace has connected at least one sender of any channel."""
    return ChannelAccount.objects.for_workspace(workspace_id).exists()


def default_sender(workspace_id: uuid.UUID, channel: str) -> ChannelAccount | None:
    """The sender used when a message names none: the oldest active account of the channel."""
    rows = ChannelAccount.objects.for_workspace(workspace_id).filter(channel=channel, status=ACTIVE)
    return rows.order_by("created_at").first()


def add_account(
    workspace_id: uuid.UUID,
    *,
    channel: str,
    provider: str,
    external_id: str,
    display_name: str = "",
    config: dict[str, Any] | None = None,
) -> ChannelAccount:
    if not external_id.strip():
        raise ValidationFailedError("invalid_sender")
    try:
        with transaction.atomic():
            return ChannelAccount.objects.create(
                workspace_id=workspace_id,
                channel=channel,
                provider=provider,
                external_id=external_id.strip(),
                display_name=display_name.strip()[:200],
                config=config or {},
            )
    except IntegrityError as exc:
        raise ConflictError("sender already exists") from exc


def get_account(workspace_id: uuid.UUID, account_id: uuid.UUID) -> ChannelAccount:
    found = ChannelAccount.objects.for_workspace(workspace_id).filter(pk=account_id).first()
    if found is None:
        raise NotFoundError("sender not found")
    return found


def find_account(workspace_id: uuid.UUID, channel: str, external_id: str) -> ChannelAccount | None:
    return (
        ChannelAccount.objects.for_workspace(workspace_id)
        .filter(channel=channel, external_id=external_id)
        .first()
    )


def update_config(
    workspace_id: uuid.UUID, account_id: uuid.UUID, changes: dict[str, Any]
) -> ChannelAccount:
    account = get_account(workspace_id, account_id)
    account.config = {**account.config, **changes}
    account.save(update_fields=["config", "updated_at"])
    return account


def remove_account(workspace_id: uuid.UUID, account_id: uuid.UUID) -> None:
    get_account(workspace_id, account_id).delete()


def contact_of_outbound(
    workspace_id: uuid.UUID, channel: str, provider_message_id: str
) -> uuid.UUID | None:
    """The contact an outbound message went to, found by the provider's id for it."""
    row = (
        Message.objects.for_workspace(workspace_id)
        .filter(channel=channel, provider_message_id=provider_message_id)
        .select_related("conversation")
        .first()
    )
    return row.conversation.contact_id if row else None
