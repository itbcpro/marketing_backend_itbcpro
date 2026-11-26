"""Comment and message inboxes: read, answer, hide, hand off, and let the AI work through them."""

import uuid
from datetime import datetime
from typing import Any

from django.db.models import Q
from django.http import HttpRequest
from ninja import Query, Router, Schema

from core.errors import ConflictError, NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from social import changes, messaging, replies, response_settings, text_rules
from social.api import account_of, user_of
from social.models import (
    CommentStatus,
    ConversationStatus,
    SocialComment,
    SocialConversation,
)
from social.response_settings import PREFIX
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["instagram-inbox"], auth=api_auth)

MAX_PAGE = 100
OPEN_COMMENT_STATUSES = [CommentStatus.NEW, CommentStatus.DRAFT_READY, CommentStatus.ESCALATED]
LEAD_STATUSES = {"new", "contacted", "qualified", "won", "lost"}


# --- comments ---------------------------------------------------------------------------------


class IgCommentOut(Schema):
    id: uuid.UUID
    external_id: str
    media_id: uuid.UUID | None
    post_permalink: str
    post_caption: str
    parent_external_id: str
    username: str
    text: str
    like_count: int
    hidden: bool
    posted_at: datetime
    status: str
    category: str
    sentiment: str
    language: str
    priority: int
    confidence: float
    reason: str
    suggested_action: str
    auto_eligible: bool
    draft: str
    reply_text: str
    handled_by: str
    handled_at: datetime | None
    action_id: uuid.UUID | None


class IgCommentPage(Schema):
    items: list[IgCommentOut]
    total: int
    open_count: int


def _comment_out(c: SocialComment) -> IgCommentOut:
    return IgCommentOut(
        id=c.pk,
        external_id=c.external_id,
        media_id=c.media_id,
        post_permalink=c.media.permalink if c.media else "",
        post_caption=c.media.caption[:140] if c.media else "",
        parent_external_id=c.parent_external_id,
        username=c.username,
        text=c.text,
        like_count=c.like_count,
        hidden=c.hidden,
        posted_at=c.posted_at,
        status=c.status,
        category=c.category,
        sentiment=c.sentiment,
        language=c.language,
        priority=c.priority,
        confidence=c.confidence,
        reason=c.reason,
        suggested_action=c.suggested_action,
        auto_eligible=c.auto_eligible,
        draft=c.draft,
        reply_text=c.reply_text,
        handled_by=c.handled_by,
        handled_at=c.handled_at,
        action_id=c.action_id,
    )


class IgCommentFilters(Schema):
    status: str = ""
    category: str = ""
    media_id: uuid.UUID | None = None
    q: str = ""
    limit: int = 30
    offset: int = 0


@router.get(
    "/accounts/{account_id}/comments", response=IgCommentPage, operation_id="instagram_comments"
)
def list_comments(
    request: HttpRequest, account_id: uuid.UUID, filters: Query[IgCommentFilters]
) -> IgCommentPage:
    account = account_of(request, account_id)
    base = SocialComment.objects.filter(account=account, from_owner=False).select_related("media")
    rows = base
    if filters.status == "open":
        rows = rows.filter(status__in=OPEN_COMMENT_STATUSES)
    elif filters.status:
        rows = rows.filter(status=filters.status)
    if filters.category:
        rows = rows.filter(category=filters.category)
    if filters.media_id:
        rows = rows.filter(media_id=filters.media_id)
    if filters.q:
        rows = rows.filter(
            Q(text__icontains=filters.q[:100]) | Q(username__icontains=filters.q[:100])
        )
    limit = max(1, min(filters.limit, MAX_PAGE))
    page = rows.order_by("-priority", "-posted_at")[
        max(0, filters.offset) : max(0, filters.offset) + limit
    ]
    return IgCommentPage(
        items=[_comment_out(c) for c in page],
        total=rows.count(),
        open_count=base.filter(status__in=OPEN_COMMENT_STATUSES).count(),
    )


def _comment_of(
    request: HttpRequest, comment_id: uuid.UUID, permission: Permission
) -> SocialComment:
    membership = resolve_workspace(request, permission)
    found = (
        SocialComment.objects.select_related("account__connection", "media")
        .filter(pk=comment_id, workspace_id=membership.workspace_id)
        .first()
    )
    if found is None:
        raise NotFoundError("comment not found")
    return found


class IgReplyIn(Schema):
    text: str


class IgActionOut(Schema):
    action_id: uuid.UUID
    status: str
    applied: bool


class IgDraftIn(Schema):
    text: str


@router.post(
    "/comments/{comment_id}/draft", response=IgCommentOut, operation_id="instagram_comment_draft"
)
def save_comment_draft(
    request: HttpRequest, comment_id: uuid.UUID, body: IgDraftIn
) -> IgCommentOut:
    """Keep an edited reply as the draft without sending it."""
    comment = _comment_of(request, comment_id, Permission.EDIT_CONTENT)
    comment.draft = body.text.strip()[:2200]
    comment.save(update_fields=["draft", "updated_at"])
    return _comment_out(comment)


@router.post(
    "/comments/{comment_id}/reply", response=IgActionOut, operation_id="instagram_comment_reply"
)
def reply_to_comment(request: HttpRequest, comment_id: uuid.UUID, body: IgReplyIn) -> IgActionOut:
    """Send a reply to the comment (or queue it for approval when the user may not approve)."""
    comment = _comment_of(request, comment_id, Permission.EDIT_CONTENT)
    if comment.status == CommentStatus.REPLIED:
        raise ConflictError("this comment was already answered")
    cfg = response_settings.load(comment.account).comments
    cleaned = replies.edit_draft(comment, body.text, cfg)
    if "empty" in cleaned.violations:
        raise ValidationFailedError("write a reply first")
    action, applied = changes.submit(
        comment.workspace_id,
        user_of(request),
        PREFIX + "reply_comment",
        replies.reply_payload(comment.account, comment, cleaned.text),
        summary=f"Reply to {comment.username}'s comment",
    )
    comment.action_id = action.pk
    comment.save(update_fields=["action_id", "updated_at"])
    return IgActionOut(action_id=action.pk, status=action.status, applied=applied)


class IgHideIn(Schema):
    hidden: bool = True


@router.post(
    "/comments/{comment_id}/hide", response=IgActionOut, operation_id="instagram_comment_hide"
)
def hide_comment(request: HttpRequest, comment_id: uuid.UUID, body: IgHideIn) -> IgActionOut:
    comment = _comment_of(request, comment_id, Permission.EDIT_CONTENT)
    action, applied = changes.submit(
        comment.workspace_id,
        user_of(request),
        PREFIX + "hide_comment",
        {
            "connection_id": str(comment.account.connection_id),
            "comment_id": comment.external_id,
            "hidden": body.hidden,
        },
        summary=f"{'Hide' if body.hidden else 'Show'} a comment by {comment.username}",
    )
    return IgActionOut(action_id=action.pk, status=action.status, applied=applied)


@router.post(
    "/comments/{comment_id}/delete", response=IgActionOut, operation_id="instagram_comment_delete"
)
def delete_comment(request: HttpRequest, comment_id: uuid.UUID) -> IgActionOut:
    """Delete permanently. Needs the right to approve irreversible actions."""
    comment = _comment_of(request, comment_id, Permission.EDIT_CONTENT)
    action, applied = changes.submit(
        comment.workspace_id,
        user_of(request),
        PREFIX + "delete_comment",
        {
            "connection_id": str(comment.account.connection_id),
            "comment_id": comment.external_id,
        },
        summary=f"Delete a comment by {comment.username}",
    )
    return IgActionOut(action_id=action.pk, status=action.status, applied=applied)


@router.post(
    "/comments/{comment_id}/ignore", response=IgCommentOut, operation_id="instagram_comment_ignore"
)
def ignore_comment(request: HttpRequest, comment_id: uuid.UUID) -> IgCommentOut:
    comment = _comment_of(request, comment_id, Permission.EDIT_CONTENT)
    return _comment_out(replies.ignore(comment))


class IgTriageOut(Schema):
    triaged: int
    sent: int


@router.post(
    "/accounts/{account_id}/comments/triage",
    response=IgTriageOut,
    operation_id="instagram_comments_triage",
)
def triage_now(request: HttpRequest, account_id: uuid.UUID) -> IgTriageOut:
    """Let the AI classify the next batch of new comments and draft replies now."""
    account = account_of(request, account_id, Permission.EDIT_CONTENT)
    counts = replies.process_account(account)
    return IgTriageOut(triaged=counts["triaged"], sent=counts["sent"])


# --- conversations ----------------------------------------------------------------------------


class IgConversationOut(Schema):
    id: uuid.UUID
    participant_username: str
    status: str
    assigned_to: uuid.UUID | None
    bot_paused: bool
    unread: bool
    needs_reply: bool
    last_message_at: datetime | None
    last_inbound_at: datetime | None
    reply_window_open: bool
    intent: str
    urgency: str
    sentiment: str
    language: str
    summary: str
    draft_reply: str
    handoff_reason: str
    lead: dict[str, Any]
    last_message: str


class IgMessageOut(Schema):
    id: uuid.UUID
    inbound: bool
    author: str
    text: str
    attachments: list[dict[str, Any]]
    sent_at: datetime


class IgConversationDetailOut(Schema):
    conversation: IgConversationOut
    messages: list[IgMessageOut]


class IgConversationPage(Schema):
    items: list[IgConversationOut]
    total: int
    waiting: int


def _conversation_out(c: SocialConversation) -> IgConversationOut:
    last = c.messages.order_by("-sent_at").first()
    return IgConversationOut(
        id=c.pk,
        participant_username=c.participant_username,
        status=c.status,
        assigned_to=c.assigned_to_id,
        bot_paused=c.bot_paused,
        unread=c.unread,
        needs_reply=c.needs_reply,
        last_message_at=c.last_message_at,
        last_inbound_at=c.last_inbound_at,
        reply_window_open=messaging.window_open(c),
        intent=c.intent,
        urgency=c.urgency,
        sentiment=c.sentiment,
        language=c.language,
        summary=c.summary,
        draft_reply=c.draft_reply,
        handoff_reason=c.handoff_reason,
        lead=c.lead,
        last_message=last.text[:160] if last else "",
    )


class IgConversationFilters(Schema):
    status: str = ""
    leads_only: bool = False
    limit: int = 30
    offset: int = 0


@router.get(
    "/accounts/{account_id}/conversations",
    response=IgConversationPage,
    operation_id="instagram_conversations",
)
def list_conversations(
    request: HttpRequest, account_id: uuid.UUID, filters: Query[IgConversationFilters]
) -> IgConversationPage:
    account = account_of(request, account_id)
    base = SocialConversation.objects.filter(account=account)
    rows = base
    if filters.status == "waiting":
        rows = rows.filter(needs_reply=True).exclude(
            status__in=[ConversationStatus.CLOSED, ConversationStatus.OPTED_OUT]
        )
    elif filters.status:
        rows = rows.filter(status=filters.status)
    if filters.leads_only:
        rows = rows.exclude(lead={})
    limit = max(1, min(filters.limit, MAX_PAGE))
    offset = max(0, filters.offset)
    page = rows.order_by("-last_message_at")[offset : offset + limit]
    return IgConversationPage(
        items=[_conversation_out(c) for c in page],
        total=rows.count(),
        waiting=base.filter(needs_reply=True)
        .exclude(status__in=[ConversationStatus.CLOSED, ConversationStatus.OPTED_OUT])
        .count(),
    )


def _conversation_of(
    request: HttpRequest, conversation_id: uuid.UUID, permission: Permission
) -> SocialConversation:
    membership = resolve_workspace(request, permission)
    found = (
        SocialConversation.objects.select_related("account__connection")
        .filter(pk=conversation_id, workspace_id=membership.workspace_id)
        .first()
    )
    if found is None:
        raise NotFoundError("conversation not found")
    return found


@router.get(
    "/conversations/{conversation_id}",
    response=IgConversationDetailOut,
    operation_id="instagram_conversation_get",
)
def get_conversation(request: HttpRequest, conversation_id: uuid.UUID) -> IgConversationDetailOut:
    conversation = _conversation_of(request, conversation_id, Permission.VIEW)
    if conversation.unread:
        conversation.unread = False
        conversation.save(update_fields=["unread", "updated_at"])
    return IgConversationDetailOut(
        conversation=_conversation_out(conversation),
        messages=[
            IgMessageOut(
                id=m.pk,
                inbound=m.inbound,
                author=m.author,
                text=m.text,
                attachments=m.attachments,
                sent_at=m.sent_at,
            )
            for m in conversation.messages.order_by("sent_at")
        ],
    )


@router.post(
    "/conversations/{conversation_id}/reply",
    response=IgActionOut,
    operation_id="instagram_conversation_reply",
)
def reply_in_conversation(
    request: HttpRequest, conversation_id: uuid.UUID, body: IgReplyIn
) -> IgActionOut:
    """Send a message. Instagram only allows it within 24 hours of the person's last message."""
    conversation = _conversation_of(request, conversation_id, Permission.EDIT_CONTENT)
    text = body.text.strip()
    if not text or len(text) > 1000:  # noqa: PLR2004 - Instagram's practical message limit
        raise ValidationFailedError("write a message of up to 1000 characters")
    if not messaging.window_open(conversation):
        raise ConflictError("the 24 hour reply window has closed")
    if conversation.status == ConversationStatus.OPTED_OUT:
        raise ConflictError("this person opted out of messages")
    action, applied = changes.submit(
        conversation.workspace_id,
        user_of(request),
        PREFIX + "send_message",
        {
            "connection_id": str(conversation.account.connection_id),
            "conversation_id": str(conversation.pk),
            "recipient_id": conversation.participant_id,
            "text": text,
        },
        summary=f"Message to {conversation.participant_username}",
    )
    return IgActionOut(action_id=action.pk, status=action.status, applied=applied)


@router.post(
    "/conversations/{conversation_id}/take-over",
    response=IgConversationOut,
    operation_id="instagram_conversation_take_over",
)
def take_over(request: HttpRequest, conversation_id: uuid.UUID) -> IgConversationOut:
    conversation = _conversation_of(request, conversation_id, Permission.EDIT_CONTENT)
    return _conversation_out(messaging.take_over(conversation, user_of(request).pk))


@router.post(
    "/conversations/{conversation_id}/hand-back",
    response=IgConversationOut,
    operation_id="instagram_conversation_hand_back",
)
def hand_back(request: HttpRequest, conversation_id: uuid.UUID) -> IgConversationOut:
    conversation = _conversation_of(request, conversation_id, Permission.EDIT_CONTENT)
    return _conversation_out(messaging.hand_back(conversation))


@router.post(
    "/conversations/{conversation_id}/close",
    response=IgConversationOut,
    operation_id="instagram_conversation_close",
)
def close_conversation(request: HttpRequest, conversation_id: uuid.UUID) -> IgConversationOut:
    conversation = _conversation_of(request, conversation_id, Permission.EDIT_CONTENT)
    conversation.status = ConversationStatus.CLOSED
    conversation.needs_reply = False
    conversation.save()
    return _conversation_out(conversation)


class IgLeadIn(Schema):
    status: str


@router.post(
    "/conversations/{conversation_id}/lead",
    response=IgConversationOut,
    operation_id="instagram_conversation_lead",
)
def set_lead_status(
    request: HttpRequest, conversation_id: uuid.UUID, body: IgLeadIn
) -> IgConversationOut:
    conversation = _conversation_of(request, conversation_id, Permission.EDIT_CONTENT)
    if body.status not in LEAD_STATUSES or not conversation.lead:
        raise ValidationFailedError("this conversation has no lead to update")
    conversation.lead = {**conversation.lead, "status": body.status}
    conversation.save(update_fields=["lead", "updated_at"])
    return _conversation_out(conversation)


@router.post(
    "/conversations/{conversation_id}/suggest",
    response=IgConversationOut,
    operation_id="instagram_conversation_suggest",
)
def suggest_reply(request: HttpRequest, conversation_id: uuid.UUID) -> IgConversationOut:
    """Ask the AI to read the conversation again and draft a reply for a person to send."""
    conversation = _conversation_of(request, conversation_id, Permission.EDIT_CONTENT)
    cfg = response_settings.load(conversation.account).messages
    analysis = messaging.analyse(conversation, cfg)
    cleaned = text_rules.clean_reply(
        analysis.reply,
        max_chars=1000,
        allowed_domains=cfg.allowed_domains,
        blocked_words=[],
        forbid_contact_details=False,
    )
    conversation.intent, conversation.urgency = analysis.intent, analysis.urgency
    conversation.sentiment, conversation.language = analysis.sentiment, analysis.language[:20]
    conversation.summary = analysis.summary
    conversation.draft_reply = cleaned.text
    messaging.merge_lead(conversation, analysis, cfg)
    conversation.save()
    return _conversation_out(conversation)
