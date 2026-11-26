"""Read side of the inbox: scoped queries, tabs, search and keyset pagination."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from core.api.pagination import MAX_LIMIT
from core.errors import ValidationFailedError
from crm.inbox.models import Conversation, ConversationRead, Message
from workspaces.models import Membership, RecordScope

TABS = ("all", "unread", "recent", "starred", "mine", "unassigned")


def visible(membership: Membership) -> QuerySet[Conversation]:
    rows = Conversation.objects.for_workspace(membership.workspace_id).filter(
        last_message_at__isnull=False
    )
    if membership.record_scope == RecordScope.ASSIGNED:
        rows = rows.filter(
            Q(assigned_to_id=membership.user_id) | Q(contact__assigned_to_id=membership.user_id)
        )
    return rows


def listing(
    membership: Membership,
    *,
    tab: str = "all",
    channel: str | None = None,
    assigned_to: uuid.UUID | None = None,
    text: str | None = None,
) -> QuerySet[Conversation]:
    if tab not in TABS:
        raise ValidationFailedError("invalid tab", errors={"tab": "invalid_value"})
    rows = visible(membership).select_related("contact")
    read = ConversationRead.objects.filter(
        conversation=OuterRef("pk"),
        user_id=membership.user_id,
        read_at__gte=OuterRef("last_inbound_at"),
    )
    unread = Q(last_inbound_at__isnull=False) & ~Exists(read)
    if tab == "unread":
        rows = rows.filter(unread)
    elif tab == "recent":
        since = timezone.now() - timedelta(days=settings.CRM_INBOX_RECENT_DAYS)
        rows = rows.filter(last_message_at__gte=since)
    elif tab == "starred":
        rows = rows.filter(starred=True)
    elif tab == "mine":
        rows = rows.filter(assigned_to_id=membership.user_id)
    elif tab == "unassigned":
        rows = rows.filter(assigned_to__isnull=True)
    if channel:
        rows = rows.filter(last_channel=channel)
    if assigned_to:
        rows = rows.filter(assigned_to_id=assigned_to)
    for term in (text or "").split()[:5]:
        rows = rows.filter(
            Q(contact__first_name__icontains=term)
            | Q(contact__last_name__icontains=term)
            | Q(contact__email__icontains=term)
            | Q(contact__phone__icontains=term)
            | Q(last_preview__icontains=term)
        )
    return rows


def unread_ids(user_id: uuid.UUID, conversation_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these conversations have inbound messages the member has not read."""
    rows = Conversation.objects.filter(pk__in=conversation_ids, last_inbound_at__isnull=False)
    read = dict(
        ConversationRead.objects.filter(
            user_id=user_id, conversation_id__in=conversation_ids
        ).values_list("conversation_id", "read_at")
    )
    return {c.pk for c in rows if c.pk not in read or read[c.pk] < c.last_inbound_at}  # type: ignore[operator]


# ---- keyset pagination ------------------------------------------------------------------------


def encode_cursor(moment: datetime, pk: uuid.UUID) -> str:
    raw = json.dumps([moment.isoformat(), str(pk)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        stamp, pk = json.loads(raw)
        return datetime.fromisoformat(stamp), uuid.UUID(pk)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ValidationFailedError("invalid cursor", errors={"cursor": "invalid_value"}) from exc


def page_newest_first(
    rows: QuerySet[Any], time_field: str, *, cursor: str | None, limit: int
) -> tuple[list[Any], str | None]:
    """Newest first by ``time_field`` then id; the cursor is the last item of the previous page."""
    size = max(1, min(limit, MAX_LIMIT))
    ordered = rows.order_by(f"-{time_field}", "-pk")
    if cursor:
        moment, pk = decode_cursor(cursor)
        ordered = ordered.filter(
            Q(**{f"{time_field}__lt": moment}) | Q(**{time_field: moment, "pk__lt": pk})
        )
    page = list(ordered[: size + 1])
    if len(page) <= size:
        return page, None
    last = page[size - 1]
    return page[:size], encode_cursor(getattr(last, time_field), last.pk)


def thread(membership: Membership, conversation_id: uuid.UUID) -> QuerySet[Message]:
    return Message.objects.for_workspace(membership.workspace_id).filter(
        conversation_id=conversation_id
    )
