"""Inbox endpoints: conversations, threads, comments, assignment and read state."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.api.pagination import DEFAULT_LIMIT, Page
from core.errors import NotFoundError, PermissionDeniedError
from crm.contacts import services as contacts
from crm.inbox import bulk, selectors, services
from crm.inbox.models import Conversation
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.models import Membership
from workspaces.permissions import Permission

router = Router(tags=["crm-inbox"], auth=api_auth)


class ConversationOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str
    assigned_to: uuid.UUID | None
    status: str
    starred: bool
    unread: bool
    last_channel: str
    last_preview: str
    last_message_at: datetime | None
    last_inbound_at: datetime | None


class MessageOut(Schema):
    id: uuid.UUID
    channel: str
    direction: str
    subject: str
    body: str
    attachments: list[dict[str, Any]]
    status: str
    error_code: str
    sent_by_type: str
    sent_by_id: str
    mentions: list[str]
    provider_ts: datetime


class CommentIn(Schema):
    body: str
    mentions: list[uuid.UUID] = []


class ConversationAssignIn(Schema):
    user_id: uuid.UUID | None


class InboxBulkIn(Schema):
    ids: list[uuid.UUID]
    action: str
    user_id: uuid.UUID | None = None


class InboxBulkOut(Schema):
    done: int
    skipped: int


class FlagsIn(Schema):
    starred: bool | None = None
    status: str | None = None


def _actor(m: Membership) -> Actor:
    return Actor.human(m.user_id)


def _name(conversation: Conversation) -> str:
    contact = conversation.contact
    return contact.full_name or contact.email or contact.phone


def _out(conversation: Conversation, unread: bool) -> ConversationOut:
    return ConversationOut(
        id=conversation.pk,
        contact_id=conversation.contact_id,
        contact_name=_name(conversation),
        assigned_to=conversation.assigned_to_id,
        status=conversation.status,
        starred=conversation.starred,
        unread=unread,
        last_channel=conversation.last_channel,
        last_preview=conversation.last_preview,
        last_message_at=conversation.last_message_at,
        last_inbound_at=conversation.last_inbound_at,
    )


def _visible(membership: Membership, conversation_id: uuid.UUID) -> Conversation:
    found = (
        selectors.visible(membership).select_related("contact").filter(pk=conversation_id).first()
    )
    if found is None:
        raise NotFoundError("conversation not found")
    return found


@router.get("/inbox/conversations", response=Page[ConversationOut], operation_id="crm_inbox_list")
def list_conversations(
    request: HttpRequest,
    tab: str = "all",
    channel: str | None = None,
    assigned_to: uuid.UUID | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[ConversationOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = services.listing(membership, tab=tab, channel=channel, assigned_to=assigned_to, text=q)
    page, next_cursor = selectors.page_newest_first(
        rows, "last_message_at", cursor=cursor, limit=limit
    )
    unread = services.unread_ids(membership.user_id, [c.pk for c in page])
    return Page[ConversationOut](
        items=[_out(c, c.pk in unread) for c in page], next_cursor=next_cursor
    )


@router.post("/inbox/conversations/bulk", response=InboxBulkOut, operation_id="crm_inbox_bulk")
def bulk_action(request: HttpRequest, payload: InboxBulkIn) -> InboxBulkOut:
    """One action on many threads; threads the member cannot see are skipped, never revealed."""
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    if not bulk.allowed(membership, payload.action) and payload.action in bulk.ACTIONS:
        raise PermissionDeniedError("not allowed")
    ids = list(dict.fromkeys(payload.ids))
    visible = list(selectors.visible(membership).filter(pk__in=ids).values_list("pk", flat=True))
    result = bulk.run(
        membership,
        _actor(membership),
        action=payload.action,
        visible_ids=visible,
        requested=len(ids),
        user_id=payload.user_id,
    )
    return InboxBulkOut(**result)


@router.get(
    "/inbox/conversations/{conversation_id}", response=ConversationOut, operation_id="crm_inbox_get"
)
def get_conversation(request: HttpRequest, conversation_id: uuid.UUID) -> ConversationOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    conversation = _visible(membership, conversation_id)
    unread = services.unread_ids(membership.user_id, [conversation.pk])
    return _out(conversation, conversation.pk in unread)


@router.get(
    "/contacts/{contact_id}/conversation",
    response=ConversationOut,
    operation_id="crm_inbox_of_contact",
)
def conversation_of_contact(request: HttpRequest, contact_id: uuid.UUID) -> ConversationOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    contact = contacts.get_visible_contact(membership, contact_id)
    conversation = (
        Conversation.objects.for_workspace(membership.workspace_id).filter(contact=contact).first()
    )
    if conversation is None or conversation.last_message_at is None:
        raise NotFoundError("conversation not found")
    return _out(_visible(membership, conversation.pk), False)


@router.get(
    "/inbox/conversations/{conversation_id}/messages",
    response=Page[MessageOut],
    operation_id="crm_inbox_messages",
)
def list_messages(
    request: HttpRequest,
    conversation_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[MessageOut]:
    """Newest first; clients reverse for display."""
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    conversation = _visible(membership, conversation_id)
    rows = selectors.thread(membership, conversation.pk)
    page, next_cursor = selectors.page_newest_first(rows, "provider_ts", cursor=cursor, limit=limit)
    return Page[MessageOut](
        items=[MessageOut.model_validate(m, from_attributes=True) for m in page],
        next_cursor=next_cursor,
    )


@router.post(
    "/inbox/conversations/{conversation_id}/comments",
    response={201: MessageOut},
    operation_id="crm_inbox_comment",
)
def add_comment(
    request: HttpRequest, conversation_id: uuid.UUID, payload: CommentIn
) -> Status[MessageOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible(membership, conversation_id)
    message = services.add_comment(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        conversation_id=conversation_id,
        body=payload.body,
        mentions=payload.mentions,
    )
    return Status(201, MessageOut.model_validate(message, from_attributes=True))


@router.post(
    "/inbox/conversations/{conversation_id}/read",
    response={204: None},
    operation_id="crm_inbox_read",
)
def mark_read(request: HttpRequest, conversation_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    _visible(membership, conversation_id)
    services.mark_read(
        workspace_id=membership.workspace_id,
        conversation_id=conversation_id,
        user_id=membership.user_id,
    )
    return Status(204, None)


@router.post(
    "/inbox/conversations/{conversation_id}/unread",
    response={204: None},
    operation_id="crm_inbox_unread",
)
def mark_unread(request: HttpRequest, conversation_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    _visible(membership, conversation_id)
    services.mark_unread(
        workspace_id=membership.workspace_id,
        conversation_id=conversation_id,
        user_id=membership.user_id,
    )
    return Status(204, None)


@router.post(
    "/inbox/conversations/{conversation_id}/assign",
    response=ConversationOut,
    operation_id="crm_inbox_assign",
)
def assign(
    request: HttpRequest, conversation_id: uuid.UUID, payload: ConversationAssignIn
) -> ConversationOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible(membership, conversation_id)
    services.assign(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        conversation_id=conversation_id,
        user_id=payload.user_id,
    )
    # After reassigning, the caller may no longer be allowed to see the thread, so read it unscoped.
    return _out(services.get_conversation(membership.workspace_id, conversation_id), False)


@router.patch(
    "/inbox/conversations/{conversation_id}",
    response=ConversationOut,
    operation_id="crm_inbox_flags",
)
def set_flags(
    request: HttpRequest, conversation_id: uuid.UUID, payload: FlagsIn
) -> ConversationOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible(membership, conversation_id)
    services.set_flags(
        workspace_id=membership.workspace_id,
        conversation_id=conversation_id,
        **payload.model_dump(exclude_unset=True),
    )
    refreshed = _visible(membership, conversation_id)
    return _out(
        refreshed, conversation_id in services.unread_ids(membership.user_id, [conversation_id])
    )
