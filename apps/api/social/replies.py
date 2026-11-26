"""Comment handling: classify new comments with AI, draft replies, apply the owner's settings.

Order of authority: the owner's response settings and the hard text rules decide what happens;
the model only classifies and drafts. An "auto" setting lets a reply or hide go out without a
person, but only when the model is confident, the text passes every rule and the hourly cap allows
it. Everything else waits as a draft for a person, one click from being sent.
"""

from datetime import timedelta
from typing import Any

from django.utils import timezone

from actions.models import ActionStatus
from core.errors import DomainError
from core.logging import get_logger
from social import ai, changes, response_settings, text_rules
from social.models import (
    CommentStatus,
    SocialAccount,
    SocialComment,
)
from social.response_settings import HIDE_KIND, REPLY_KIND
from social.schemas import CommentSettings, CommentTriage, CommentTriageResult

log = get_logger(__name__)

PROMPT_KEY = "social/instagram-comments"
BATCH_SIZE = 20
ESCALATE_CATEGORIES = {"complaint", "sensitive"}
SAFETY_CATEGORIES = {"spam", "abuse"}
URGENT = 3


def pending(
    account: SocialAccount, cfg: CommentSettings, limit: int = BATCH_SIZE
) -> list[SocialComment]:
    since = timezone.now() - timedelta(days=cfg.ignore_older_than_days)
    return list(
        SocialComment.objects.filter(
            account=account,
            from_owner=False,
            status=CommentStatus.NEW,
            triaged_at__isnull=True,
            posted_at__gte=since,
        )
        .select_related("media")
        .order_by("posted_at")[:limit]
    )


def _limits(cfg: CommentSettings) -> str:
    domains = ", ".join(cfg.allowed_domains) or "none (do not include links)"
    emoji = {
        "none": "no emojis",
        "light": "at most two emojis",
        "brand": "emojis as the brand voice uses them",
    }[cfg.emoji]
    return (
        f"- Maximum {cfg.max_reply_chars} characters per reply.\n"
        f"- Allowed link domains: {domains}.\n- Emoji policy: {emoji}.\n"
        f"- Never use these words: {', '.join(cfg.blocked_words) or 'none listed'}."
    )


def _rules(cfg: CommentSettings) -> str:
    lines = [f"- {category}: {mode}" for category, mode in sorted(cfg.rules.items())]
    lines.append(f"- hide spam automatically: {cfg.hide_spam}")
    lines.append(f"- hide abuse automatically: {cfg.hide_abuse}")
    lines.append("- spam, abuse and sensitive comments are never answered")
    return "\n".join(lines)


def _comment_view(index: int, comment: SocialComment) -> dict[str, Any]:
    parent = (
        SocialComment.objects.filter(
            account=comment.account, external_id=comment.parent_external_id
        ).first()
        if comment.parent_external_id
        else None
    )
    return {
        "index": index,
        "author": comment.username,
        "text": comment.text[:1000],
        "replying_to": parent.text[:300] if parent else None,
        "post": str(comment.media_id) if comment.media_id else None,
    }


def _posts_view(comments: list[SocialComment]) -> dict[str, str]:
    return {str(c.media_id): c.media.caption[:300] for c in comments if c.media is not None}


def triage(
    account: SocialAccount, comments: list[SocialComment], cfg: CommentSettings
) -> list[CommentTriage]:
    result = ai.run_prompt(
        account.workspace_id,
        PROMPT_KEY,
        CommentTriageResult,
        memory_query="instagram comment replies tone",
        user_message="Handle the comments.",
        tone=cfg.tone,
        rules=_rules(cfg),
        limits=_limits(cfg),
        posts=ai.dumps(_posts_view(comments)),
        comments=ai.untrusted(
            "instagram_comments", [_comment_view(i, c) for i, c in enumerate(comments)]
        ),
    )
    return [item for item in result.items if 0 <= item.index < len(comments)]


def _decide_status(item: CommentTriage, cfg: CommentSettings) -> tuple[str, str]:
    """``(status, suggested action)`` from the model's view and the settings."""
    if item.action == "ignore":
        return CommentStatus.IGNORED, "ignore"
    if item.category in SAFETY_CATEGORIES:
        return CommentStatus.DRAFT_READY, "hide"
    if item.category in ESCALATE_CATEGORIES or item.action == "escalate" or item.priority >= URGENT:
        return CommentStatus.ESCALATED, "escalate"
    if cfg.rules.get(item.category, "off") == "off":
        return CommentStatus.IGNORED, "ignore"
    return CommentStatus.DRAFT_READY, "reply"


def _auto_ok(
    item: CommentTriage, cfg: CommentSettings, suggested: str, violations: list[str]
) -> bool:
    if item.confidence < cfg.min_confidence:
        return False
    if suggested == "hide":
        return (item.category == "spam" and cfg.hide_spam) or (
            item.category == "abuse" and cfg.hide_abuse
        )
    return suggested == "reply" and cfg.rules.get(item.category) == "auto" and not violations


def apply_triage(comment: SocialComment, item: CommentTriage, cfg: CommentSettings) -> None:
    status, suggested = _decide_status(item, cfg)
    cleaned = (
        text_rules.clean_reply(
            item.reply,
            max_chars=cfg.max_reply_chars,
            allowed_domains=cfg.allowed_domains,
            blocked_words=cfg.blocked_words,
            forbid_contact_details=True,
            emoji=cfg.emoji,
            sign_off=cfg.sign_off,
        )
        if item.reply and suggested in {"reply", "escalate"}
        else text_rules.Cleaned("")
    )
    if suggested == "reply" and not cleaned.text:
        status, suggested = CommentStatus.ESCALATED, "escalate"
    comment.category = item.category
    comment.sentiment = item.sentiment
    comment.language = item.language[:20]
    comment.priority = item.priority
    comment.confidence = item.confidence
    comment.reason = item.reason[:300]
    comment.status = status
    comment.suggested_action = suggested
    comment.draft = cleaned.text
    comment.auto_eligible = _auto_ok(item, cfg, suggested, cleaned.violations)
    comment.triaged_at = timezone.now()
    comment.handled_by = "ai" if status == CommentStatus.IGNORED else ""
    comment.save()


def process_account(account: SocialAccount) -> dict[str, int]:
    """Classify the next batch of new comments and queue what the settings allow."""
    cfg = response_settings.load(account).comments
    if not cfg.enabled:
        return {"triaged": 0, "sent": 0}
    comments = pending(account, cfg)
    triaged = 0
    if comments:
        try:
            items = triage(account, comments, cfg)
        except DomainError as exc:
            log.warning("comment_triage_failed", code=exc.code.value)
            return {"triaged": 0, "sent": 0}
        by_index = {item.index: item for item in items}
        for index, comment in enumerate(comments):
            if index in by_index:
                apply_triage(comment, by_index[index], cfg)
                triaged += 1
    return {"triaged": triaged, "sent": dispatch_auto(account, cfg)}


def _sent_last_hour(account: SocialAccount) -> int:
    since = timezone.now() - timedelta(hours=1)
    return SocialComment.objects.filter(
        account=account,
        handled_by="ai",
        handled_at__gte=since,
        status__in=[CommentStatus.REPLIED, CommentStatus.HIDDEN],
    ).count()


def _proposed_last_hour(account: SocialAccount) -> int:
    since = timezone.now() - timedelta(hours=1)
    return SocialComment.objects.filter(
        account=account, action_id__isnull=False, updated_at__gte=since, handled_by=""
    ).count()


def dispatch_auto(account: SocialAccount, cfg: CommentSettings | None = None) -> int:
    """Propose the replies and hides the settings allow, oldest first, within the hourly cap."""
    cfg = cfg or response_settings.load(account).comments
    budget = cfg.max_auto_per_hour - _sent_last_hour(account) - _proposed_last_hour(account)
    if budget <= 0:
        return 0
    due = timezone.now() - timedelta(minutes=cfg.delay_minutes)
    candidates = SocialComment.objects.filter(
        account=account,
        auto_eligible=True,
        status=CommentStatus.DRAFT_READY,
        action_id__isnull=True,
        posted_at__lte=due,
    ).order_by("posted_at")[:budget]
    sent = 0
    for comment in candidates:
        sent += int(_propose(account, comment))
    return sent


def _propose(account: SocialAccount, comment: SocialComment) -> bool:
    if comment.suggested_action == "hide":
        kind, summary = HIDE_KIND, f"Hide a {comment.category} comment by {comment.username}"
        payload: dict[str, Any] = {
            "connection_id": str(account.connection_id),
            "comment_id": comment.external_id,
            "hidden": True,
        }
    else:
        kind, summary = REPLY_KIND, f"Reply to {comment.username}'s comment"
        payload = {
            "connection_id": str(account.connection_id),
            "comment_id": comment.external_id,
            "text": comment.draft,
        }
    action = changes.propose_as_responder(account.workspace_id, kind, payload, summary=summary)
    comment.action_id = action.pk
    comment.auto_eligible = False
    if action.status == ActionStatus.POLICY_BLOCKED:
        comment.status = CommentStatus.ESCALATED
        comment.reason = "blocked by a safety rule"
    comment.save()
    return action.status in {ActionStatus.APPROVED, ActionStatus.AWAITING_APPROVAL}


# --- human actions ----------------------------------------------------------------------------


def edit_draft(comment: SocialComment, text: str, cfg: CommentSettings) -> text_rules.Cleaned:
    return text_rules.clean_reply(
        text,
        max_chars=2200,
        allowed_domains=cfg.allowed_domains,
        blocked_words=cfg.blocked_words,
        forbid_contact_details=False,
    )


def reply_payload(account: SocialAccount, comment: SocialComment, text: str) -> dict[str, Any]:
    return {
        "connection_id": str(account.connection_id),
        "comment_id": comment.external_id,
        "text": text,
    }


def ignore(comment: SocialComment) -> SocialComment:
    comment.status = CommentStatus.IGNORED
    comment.handled_by = "human"
    comment.handled_at = timezone.now()
    comment.auto_eligible = False
    comment.save()
    return comment
