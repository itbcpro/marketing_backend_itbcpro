"""Bridge: copy Instagram direct messages (kept by the social module) into the unified inbox.

The social module owns Instagram tokens, sync and its own AI inbox; this bridge only reads its
message table and writes inbox rows, so nothing is duplicated and no token is shared. Copying is
idempotent (the inbox stores each provider message id once), so a run can safely be repeated.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db.models import Q

from audit.actors import Actor
from crm.consent.models import Channel
from crm.contacts.models import IdentityChannel
from crm.inbox import conversations, ingest
from crm.inbox.models import BridgeCursor, ChannelAccount
from social.models import SocialAccount, SocialMessage

SOURCE = "instagram"
PROVIDER = "instagram"


def _account(workspace_id: uuid.UUID, social: SocialAccount) -> ChannelAccount:
    row, _ = ChannelAccount.objects.get_or_create(
        workspace_id=workspace_id,
        channel=Channel.INSTAGRAM,
        external_id=social.external_id,
        defaults={
            "provider": PROVIDER,
            "display_name": f"@{social.username}" if social.username else "",
        },
    )
    return row


def _copy_one(
    workspace_id: uuid.UUID, message: SocialMessage, accounts: dict[uuid.UUID, ChannelAccount]
) -> bool:
    """Copy one message; returns True when a new inbox message was stored."""
    thread = message.conversation
    person = thread.participant_id or thread.external_id
    if not person or not message.external_id:
        return False
    account = accounts.get(thread.account_id)
    if account is None:
        account = accounts[thread.account_id] = _account(workspace_id, thread.account)
    if message.inbound:
        result = ingest.ingest(
            workspace_id=workspace_id,
            item=ingest.InboundMessage(
                channel=Channel.INSTAGRAM,
                provider_message_id=message.external_id,
                sender_channel=IdentityChannel.INSTAGRAM,
                sender_value=person,
                provider_ts=message.sent_at,
                body=message.text,
                attachments=message.attachments,
                sender_name=thread.participant_username,
                channel_account_id=account.pk,
            ),
        )
        return result.created
    contact = ingest.resolve_contact(
        workspace_id,
        channel=Channel.INSTAGRAM,
        sender_channel=IdentityChannel.INSTAGRAM,
        value=person,
        name=thread.participant_username,
    )
    _, created = conversations.mirror_outbound(
        workspace_id=workspace_id,
        contact=contact,
        channel=Channel.INSTAGRAM,
        provider_message_id=message.external_id,
        body=message.text,
        provider_ts=message.sent_at,
        sent_by=Actor.agent(message.author)
        if message.author not in {"owner", "user"}
        else Actor.system("instagram"),
        attachments=message.attachments,
        channel_account_id=account.pk,
    )
    return created


def run(workspace_id: uuid.UUID) -> dict[str, int]:
    """Copy every Instagram message newer than the saved cursor, in order, chunk by chunk."""
    cursor, _ = BridgeCursor.objects.get_or_create(workspace_id=workspace_id, source=SOURCE)
    accounts: dict[uuid.UUID, ChannelAccount] = {}
    copied = seen = 0
    size = settings.CRM_BULK_CHUNK_SIZE
    while True:
        rows = SocialMessage.objects.for_workspace(workspace_id).select_related(
            "conversation__account"
        )
        if cursor.last_created_at is not None and cursor.last_id is not None:
            rows = rows.filter(
                Q(created_at__gt=cursor.last_created_at)
                | Q(created_at=cursor.last_created_at, pk__gt=cursor.last_id)
            )
        batch: list[Any] = list(rows.order_by("created_at", "pk")[:size])
        if not batch:
            break
        for message in batch:
            copied += int(_copy_one(workspace_id, message, accounts))
            seen += 1
        cursor.last_created_at, cursor.last_id = batch[-1].created_at, batch[-1].pk
        cursor.save()
    return {"seen": seen, "copied": copied}


def workspaces_with_instagram() -> list[uuid.UUID]:
    return list(SocialAccount.objects.values_list("workspace_id", flat=True).distinct())
