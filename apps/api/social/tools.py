"""Agent tools for Instagram: read tools over the local copy plus one proposing tool per action.

Reading comments and messages is flagged untrusted: the output is fenced as data and the run becomes
tainted, which removes automatic approval for everything it proposes afterwards. Drafting a post is
internal (nothing is published); the write tools only propose actions.
"""

from datetime import datetime
from typing import Any

from django.utils import timezone
from pydantic import BaseModel, Field

from actions.registry import get_action_kind
from agents.tools import ToolContext, action_tool, read_tool, registry
from connections.models import ConnectionStatus
from core.errors import NotFoundError, ValidationFailedError
from social import analytics, checks
from social.models import (
    CommentStatus,
    ConversationStatus,
    DraftStatus,
    MediaKind,
    PostDraft,
    SocialAccount,
    SocialComment,
    SocialConversation,
    SocialMedia,
)
from social.response_settings import PREFIX

WRITE_TOOLS = {
    "instagram_reply_comment": "reply_comment",
    "instagram_hide_comment": "hide_comment",
    "instagram_delete_comment": "delete_comment",
    "instagram_set_comments_enabled": "set_comments_enabled",
    "instagram_send_message": "send_message",
}
TOOL_NAMES = [
    "instagram_overview",
    "instagram_stats",
    "instagram_posts",
    "instagram_inbox",
    "instagram_draft_post",
    *WRITE_TOOLS,
]
INBOX_LIMIT = 30
POST_LIMIT = 30


def _account(ctx: ToolContext) -> SocialAccount:
    found = (
        SocialAccount.objects.filter(
            workspace_id=ctx.workspace_id,
            connection__status__in=[ConnectionStatus.ACTIVE, ConnectionStatus.DEGRADED],
        )
        .select_related("connection")
        .order_by("created_at")
        .first()
    )
    if found is None:
        raise NotFoundError("No active Instagram account in this workspace")
    return found


class Empty(BaseModel):
    pass


def _overview(ctx: ToolContext, args: Empty) -> dict[str, Any]:
    account = _account(ctx)
    return {
        "connection_id": str(account.connection_id),
        "username": account.username,
        "type": account.account_type,
        "followers": account.followers_count,
        "posts": account.media_count,
        "last_synced_at": account.last_synced_at.isoformat() if account.last_synced_at else None,
    }


class StatsInput(BaseModel):
    days: int = Field(default=90, ge=7, le=365)


def _stats(ctx: ToolContext, args: StatsInput) -> dict[str, Any]:
    return analytics.compute_stats(_account(ctx), args.days)


class PostsInput(BaseModel):
    kind: str | None = Field(default=None, description="image, carousel, video, reel or story")
    limit: int = Field(default=15, ge=1, le=POST_LIMIT)


def _posts(ctx: ToolContext, args: PostsInput) -> list[dict[str, Any]]:
    rows = SocialMedia.objects.filter(account=_account(ctx), removed_at__isnull=True)
    if args.kind:
        rows = rows.filter(kind=args.kind)
    return [
        {
            "id": m.external_id,
            "kind": m.kind,
            "posted_at": m.posted_at.isoformat(),
            "caption": m.caption[:300],
            "metrics": m.metrics,
            "comments": m.comments_count,
            "permalink": m.permalink,
        }
        for m in rows[: args.limit]
    ]


def _last_inbound_text(conversation: SocialConversation) -> str:
    last = conversation.messages.filter(inbound=True).order_by("-sent_at").first()
    return last.text[:500] if last else ""


def _inbox(ctx: ToolContext, args: Empty) -> dict[str, Any]:
    account = _account(ctx)
    comments = SocialComment.objects.filter(
        account=account,
        from_owner=False,
        status__in=[CommentStatus.NEW, CommentStatus.DRAFT_READY, CommentStatus.ESCALATED],
    ).order_by("-posted_at")[:INBOX_LIMIT]
    conversations = SocialConversation.objects.filter(
        account=account, needs_reply=True, status=ConversationStatus.OPEN
    )[:INBOX_LIMIT]
    return {
        "connection_id": str(account.connection_id),
        "comments": [
            {
                "comment_id": c.external_id,
                "author": c.username,
                "text": c.text[:500],
                "category": c.category,
                "post": c.media.external_id if c.media else None,
            }
            for c in comments.select_related("media")
        ],
        "conversations": [
            {
                "conversation_id": str(c.pk),
                "recipient_id": c.participant_id,
                "person": c.participant_username,
                "summary": c.summary,
                "last_message": _last_inbound_text(c),
            }
            for c in conversations
        ],
    }


class DraftInput(BaseModel):
    kind: str = Field(description="image, carousel, reel or story")
    title: str = Field(max_length=120)
    caption: str = Field(max_length=2200, description="Hook first, one call to action")
    hashtags: list[str] = Field(default_factory=list, max_length=10)
    first_comment: str = Field(default="", max_length=600)
    brief: str = Field(default="", max_length=2000, description="Notes for the person reviewing")
    scheduled_at: datetime | None = Field(
        default=None, description="Suggested publishing time (ISO 8601, with time zone)"
    )


def _draft(ctx: ToolContext, args: DraftInput) -> dict[str, Any]:
    if args.kind not in {k.value for k in MediaKind}:
        raise ValidationFailedError("unknown post format")
    if args.scheduled_at and args.scheduled_at <= timezone.now():
        raise ValidationFailedError("the suggested time is in the past")
    account = _account(ctx)
    draft = PostDraft.objects.create(
        workspace_id=ctx.workspace_id,
        account=account,
        kind=args.kind,
        status=DraftStatus.DRAFTED,
        title=args.title,
        caption=args.caption,
        hashtags=[h if h.startswith("#") else f"#{h}" for h in args.hashtags],
        first_comment=args.first_comment,
        brief=args.brief,
        scheduled_at=args.scheduled_at,
        origin="ai",
        ai={"agent": ctx.agent_name},
    )
    draft.checks = checks.run_checks(draft)
    draft.save(update_fields=["checks", "updated_at"])
    return {
        "draft_id": str(draft.pk),
        "status": "draft saved; a person must add pictures, approve and schedule it",
        "checks": draft.checks,
    }


def register_tools() -> None:
    registry.register(
        read_tool(
            "instagram_overview",
            "The connected Instagram account (followers, post count, last sync) and the "
            "connection_id the write tools need.",
            Empty,
            _overview,
        )
    )
    registry.register(
        read_tool(
            "instagram_stats",
            "Measured results of the account over the last N days: medians per format, weekday "
            "and time of day with sample sizes, best and weakest posts, audience, community.",
            StatsInput,
            _stats,
        )
    )
    registry.register(
        read_tool(
            "instagram_posts",
            "Recent posts, reels and carousels with their metrics (reach, views, saves, shares).",
            PostsInput,
            _posts,
        )
    )
    registry.register(
        read_tool(
            "instagram_inbox",
            "New comments and direct messages that still need an answer. Text from outside.",
            Empty,
            _inbox,
            untrusted_output=True,
        )
    )
    registry.register(
        read_tool(
            "instagram_draft_post",
            "Save a post as a draft on the content calendar. Nothing is published; a person "
            "reviews, adds pictures, approves and schedules it.",
            DraftInput,
            _draft,
        )
    )
    for tool_name, suffix in WRITE_TOOLS.items():
        kind = PREFIX + suffix
        registry.register(action_tool(tool_name, kind, get_action_kind(kind).description))
