"""Keep the local copy of an Instagram account current (profile, media, insights, inbox).

Everything here only reads from Instagram. A failure on one item (an unsupported metric on an old
post, one broken thread) never stops the rest; authorisation and rate-limit failures do, because
carrying on would only repeat them.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from django.db import IntegrityError
from django.utils import timezone

from connections.models import Connection
from core.errors import AuthExpiredError, DomainError, RateLimitedError
from core.logging import get_logger
from social.instagram import factory
from social.instagram.client import InstagramClient
from social.models import (
    AccountInsight,
    CommentStatus,
    ConversationStatus,
    MediaKind,
    MediaMetricSnapshot,
    SocialAccount,
    SocialComment,
    SocialConversation,
    SocialMedia,
    SocialMessage,
)

log = get_logger(__name__)

MEDIA_LIMIT = 100
METRIC_AGE_DAYS = 30
METRIC_CALL_BUDGET = 60
COMMENT_WINDOW_DAYS = 30
COMMENT_MEDIA_BUDGET = 40
CONVERSATION_LIMIT = 25
INSIGHT_WINDOW_DAYS = 28
AUDIENCE_REFRESH_DAYS = 7
MIN_COMMENT_HEAD = 1
WEBHOOK_FIELDS = ["comments", "messages", "mentions", "message_reactions"]
SECONDS_PER_DAY = 86_400

FEED_METRICS = ["reach", "likes", "comments", "saved", "shares", "total_interactions", "views"]
REEL_METRICS = [*FEED_METRICS, "ig_reels_avg_watch_time", "ig_reels_video_view_total_time"]
STORY_METRICS = ["reach", "replies", "shares", "total_interactions", "views"]
METRIC_FALLBACKS = (["reach", "likes", "comments", "saved", "shares"], ["reach"])
ACCOUNT_METRICS = [
    "reach",
    "views",
    "accounts_engaged",
    "total_interactions",
    "likes",
    "comments",
    "saves",
    "shares",
    "replies",
    "reposts",
    "follows_and_unfollows",
    "profile_links_taps",
]
DEMOGRAPHIC_BREAKDOWNS = ("age", "gender", "city", "country")
METRIC_RENAMES = {
    "saved": "saves",
    "total_interactions": "interactions",
    "ig_reels_avg_watch_time": "avg_watch_seconds",
    "ig_reels_video_view_total_time": "total_watch_seconds",
}
MS_METRICS = {"avg_watch_seconds", "total_watch_seconds"}


def parse_time(value: object) -> datetime:
    """Instagram timestamps look like ``2024-05-10T12:00:00+0000``."""
    text = str(value or "")
    for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(text, pattern)  # noqa: DTZ007 - the pattern carries the zone
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return timezone.now()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def kind_of(item: dict[str, Any]) -> str:
    product = str(item.get("media_product_type") or "").upper()
    media_type = str(item.get("media_type") or "").upper()
    if product == "STORY":
        return MediaKind.STORY
    if product == "REELS":
        return MediaKind.REEL
    if media_type == "CAROUSEL_ALBUM":
        return MediaKind.CAROUSEL
    if media_type == "VIDEO":
        return MediaKind.VIDEO
    return MediaKind.IMAGE


# --- account ----------------------------------------------------------------------------------


def upsert_account(connection: Connection, info: dict[str, Any]) -> SocialAccount:
    account, _ = SocialAccount.objects.get_or_create(
        connection=connection,
        defaults={
            "workspace_id": connection.workspace_id,
            "external_id": connection.external_account_id,
        },
    )
    account.username = str(info.get("username") or account.username)
    account.name = str(info.get("name") or "")[:200]
    account.account_type = str(info.get("account_type") or "")
    account.profile_picture_url = str(info.get("profile_picture_url") or "")[:2048]
    account.biography = str(info.get("biography") or "")
    account.website = str(info.get("website") or "")[:500]
    account.followers_count = int(info.get("followers_count") or 0)
    account.follows_count = int(info.get("follows_count") or 0)
    account.media_count = int(info.get("media_count") or 0)
    account.save()
    return account


# --- media ------------------------------------------------------------------------------------


def upsert_media(account: SocialAccount, item: dict[str, Any]) -> SocialMedia:
    media, _ = SocialMedia.objects.get_or_create(
        account=account,
        external_id=str(item["id"]),
        defaults={
            "workspace_id": account.workspace_id,
            "kind": kind_of(item),
            "posted_at": parse_time(item.get("timestamp")),
        },
    )
    media.kind = kind_of(item)
    media.caption = str(item.get("caption") or "")
    media.permalink = str(item.get("permalink") or "")[:500]
    media.media_url = str(item.get("media_url") or "")[:2048]
    media.thumbnail_url = str(item.get("thumbnail_url") or "")[:2048]
    media.like_count = int(item.get("like_count") or 0)
    media.comments_count = int(item.get("comments_count") or 0)
    media.comments_enabled = item.get("is_comment_enabled", True) is not False
    media.children = [
        {"id": c.get("id"), "type": c.get("media_type"), "url": c.get("media_url")}
        for c in (item.get("children") or {}).get("data", [])
    ]
    media.removed_at = None
    media.save()
    return media


def metrics_for(kind: str) -> list[str]:
    if kind == MediaKind.REEL:
        return REEL_METRICS
    return STORY_METRICS if kind == MediaKind.STORY else FEED_METRICS


def normalise_metrics(raw: dict[str, float]) -> dict[str, float | None]:
    metrics: dict[str, float | None] = {}
    for key, value in raw.items():
        name = METRIC_RENAMES.get(key, key)
        metrics[name] = round(value / 1000, 2) if name in MS_METRICS else value
    reach = metrics.get("reach")
    interactions = metrics.get("interactions")
    metrics["engagement_rate"] = (
        round(interactions / reach, 4) if reach and interactions is not None else None
    )
    return metrics


def fetch_media_metrics(client: InstagramClient, media: SocialMedia) -> dict[str, float]:
    """Try the full metric set, then smaller ones: old or special posts reject some metrics."""
    attempts = [metrics_for(media.kind), *METRIC_FALLBACKS]
    for names in attempts:
        try:
            return client.media_insights(media.external_id, names)
        except AuthExpiredError, RateLimitedError:
            raise
        except DomainError:
            continue
    return {}


def sync_media_metrics(client: InstagramClient, account: SocialAccount, now: datetime) -> int:
    """Refresh metrics of recent posts first, then of posts that never got any."""
    cutoff = now - timedelta(days=METRIC_AGE_DAYS)
    recent = list(account.media.filter(posted_at__gte=cutoff).order_by("-posted_at"))
    unmeasured = list(account.media.filter(metrics_synced_at__isnull=True, posted_at__lt=cutoff))
    done = 0
    for media in [*recent, *unmeasured][:METRIC_CALL_BUDGET]:
        raw = fetch_media_metrics(client, media)
        if not raw:
            continue
        media.metrics = normalise_metrics(raw)
        media.metrics_synced_at = now
        media.save(update_fields=["metrics", "metrics_synced_at", "updated_at"])
        _snapshot(media, now)
        done += 1
    return done


def _snapshot(media: SocialMedia, now: datetime) -> None:
    age_hours = int((now - media.posted_at).total_seconds() // 3600)
    MediaMetricSnapshot.objects.update_or_create(
        media=media,
        day=now.date(),
        defaults={
            "workspace_id": media.workspace_id,
            "age_hours": age_hours,
            "metrics": media.metrics,
        },
    )


def sync_media(client: InstagramClient, account: SocialAccount, now: datetime) -> int:
    items = client.list_media(max_items=MEDIA_LIMIT)
    stories: list[dict[str, Any]] = []
    try:
        stories = client.list_stories()
    except AuthExpiredError, RateLimitedError:
        raise
    except DomainError:
        log.info("instagram_stories_unavailable", account=str(account.pk))
    seen = {str(i["id"]) for i in [*items, *stories]}
    for item in [*items, *stories]:
        upsert_media(account, item)
    # Posts that vanished from the newest page were deleted (or fell out of the window).
    window_start = min((parse_time(i.get("timestamp")) for i in items), default=now)
    account.media.filter(posted_at__gte=window_start, removed_at__isnull=True).exclude(
        external_id__in=seen
    ).update(removed_at=now)
    return len(seen)


# --- account insights -------------------------------------------------------------------------


def _totals(client: InstagramClient, since: int, until: int) -> dict[str, float]:
    try:
        return client.account_totals(ACCOUNT_METRICS, since=since, until=until)
    except AuthExpiredError, RateLimitedError:
        raise
    except DomainError:
        pass
    found: dict[str, float] = {}
    for metric in ACCOUNT_METRICS:
        try:
            found.update(client.account_totals([metric], since=since, until=until))
        except AuthExpiredError, RateLimitedError:
            raise
        except DomainError:
            continue
    return found


def _series(client: InstagramClient, metric: str, since: int, until: int) -> list[dict[str, Any]]:
    try:
        rows = client.account_series(metric, since=since, until=until)
    except AuthExpiredError, RateLimitedError:
        raise
    except DomainError:
        return []
    return [
        {"date": str(parse_time(r.get("end_time")).date()), "value": r.get("value", 0)}
        for r in rows
    ]


def sync_account_insights(client: InstagramClient, account: SocialAccount, now: datetime) -> bool:
    until = int(now.timestamp())
    since = int((now - timedelta(days=INSIGHT_WINDOW_DAYS)).timestamp())
    totals = _totals(client, since, until)
    series = {
        "reach": _series(client, "reach", since, until),
        "follower_count": _series(client, "follower_count", since, until),
    }
    if not totals and not any(series.values()):
        return False
    AccountInsight.objects.update_or_create(
        account=account,
        day=now.date(),
        defaults={
            "workspace_id": account.workspace_id,
            "window_days": INSIGHT_WINDOW_DAYS,
            "followers_count": account.followers_count,
            "totals": totals,
            "series": series,
        },
    )
    return True


def sync_audience(client: InstagramClient, account: SocialAccount, now: datetime) -> bool:
    """Demographics change slowly and need 100+ followers, so they refresh once a week."""
    last = account.audience_synced_at
    if last and now - last < timedelta(days=AUDIENCE_REFRESH_DAYS):
        return False
    audience: dict[str, Any] = {"followers": {}, "engaged": {}, "online_hours": {}}
    for label, metric in (
        ("followers", "follower_demographics"),
        ("engaged", "engaged_audience_demographics"),
    ):
        for breakdown in DEMOGRAPHIC_BREAKDOWNS:
            try:
                audience[label][breakdown] = client.demographics(metric, breakdown)
            except AuthExpiredError, RateLimitedError:
                raise
            except DomainError:
                audience[label][breakdown] = []
    try:
        audience["online_hours"] = client.online_followers()
    except AuthExpiredError, RateLimitedError:
        raise
    except DomainError:
        audience["online_hours"] = {}
    account.audience = audience
    account.audience_synced_at = now
    account.save(update_fields=["audience", "audience_synced_at", "updated_at"])
    return True


# --- comments ---------------------------------------------------------------------------------


def _is_owner(account: SocialAccount, username: str, author_id: str) -> bool:
    return bool(
        (username and username == account.username)
        or (author_id and author_id == account.external_id)
    )


def upsert_comment(
    account: SocialAccount, media: SocialMedia | None, item: dict[str, Any], parent_id: str
) -> tuple[SocialComment, bool]:
    author = item.get("from") or {}
    username = str(item.get("username") or author.get("username") or "")
    author_id = str(author.get("id") or "")
    owner = _is_owner(account, username, author_id)
    try:
        comment, created = SocialComment.objects.get_or_create(
            account=account,
            external_id=str(item["id"]),
            defaults={
                "workspace_id": account.workspace_id,
                "media": media,
                "parent_external_id": parent_id,
                "username": username[:100],
                "author_id": author_id[:100],
                "text": str(item.get("text") or ""),
                "posted_at": parse_time(item.get("timestamp")),
                "from_owner": owner,
                "status": CommentStatus.IGNORED if owner else CommentStatus.NEW,
                "handled_by": "owner" if owner else "",
            },
        )
    except IntegrityError:
        comment, created = (
            SocialComment.objects.get(account=account, external_id=str(item["id"])),
            False,
        )
    comment.like_count = int(item.get("like_count") or 0)
    comment.hidden = bool(item.get("hidden"))
    comment.save(update_fields=["like_count", "hidden", "updated_at"])
    return comment, created


def sync_comments_of(
    client: InstagramClient, account: SocialAccount, media: SocialMedia
) -> tuple[int, int]:
    """Top-level comments and their replies; returns ``(seen, new)``."""
    seen = new = 0
    for item in client.list_comments(media.external_id):
        comment, created = upsert_comment(account, media, item, "")
        seen, new = seen + 1, new + int(created)
        owner_replied = False
        for reply in (item.get("replies") or {}).get("data", []):
            child, child_created = upsert_comment(account, media, reply, comment.external_id)
            seen, new = seen + 1, new + int(child_created)
            owner_replied = owner_replied or child.from_owner
        if owner_replied and comment.status in {CommentStatus.NEW, CommentStatus.DRAFT_READY}:
            comment.status = CommentStatus.REPLIED
            comment.handled_by = comment.handled_by or "human"
            comment.save(update_fields=["status", "handled_by", "updated_at"])
    return seen, new


def sync_comments(client: InstagramClient, account: SocialAccount, now: datetime) -> dict[str, int]:
    cutoff = now - timedelta(days=COMMENT_WINDOW_DAYS)
    targets = account.media.filter(
        posted_at__gte=cutoff, comments_count__gt=0, removed_at__isnull=True
    ).order_by("-posted_at")[:COMMENT_MEDIA_BUDGET]
    seen = new = 0
    for media in targets:
        try:
            found, fresh = sync_comments_of(client, account, media)
        except AuthExpiredError, RateLimitedError:
            raise
        except DomainError:
            continue
        seen, new = seen + found, new + fresh
    return {"comments": seen, "new_comments": new}


# --- messages ---------------------------------------------------------------------------------


def _participant(account: SocialAccount, conversation: dict[str, Any]) -> dict[str, Any]:
    for person in (conversation.get("participants") or {}).get("data", []):
        if (
            str(person.get("id")) != account.external_id
            and person.get("username") != account.username
        ):
            return dict(person)
    return {}


def upsert_conversation(account: SocialAccount, item: dict[str, Any]) -> SocialConversation:
    person = _participant(account, item)
    conversation, _ = SocialConversation.objects.get_or_create(
        account=account,
        external_id=str(item["id"]),
        defaults={"workspace_id": account.workspace_id},
    )
    conversation.participant_id = str(person.get("id") or conversation.participant_id)
    conversation.participant_username = str(
        person.get("username") or conversation.participant_username
    )[:100]
    conversation.save()
    return conversation


def upsert_message(
    account: SocialAccount, conversation: SocialConversation, item: dict[str, Any]
) -> tuple[SocialMessage, bool]:
    sender = str((item.get("from") or {}).get("id") or "")
    inbound = sender != account.external_id
    message, created = SocialMessage.objects.get_or_create(
        conversation=conversation,
        external_id=str(item["id"]),
        defaults={
            "workspace_id": account.workspace_id,
            "inbound": inbound,
            "author": "user" if inbound else "human",
            "text": str(item.get("message") or ""),
            "attachments": (item.get("attachments") or {}).get("data", []),
            "sent_at": parse_time(item.get("created_time")),
        },
    )
    return message, created


def refresh_conversation_state(conversation: SocialConversation, new_inbound: bool) -> None:
    last = conversation.messages.order_by("-sent_at").first()
    if last is None:
        return
    inbound = conversation.messages.filter(inbound=True).order_by("-sent_at").first()
    outbound = conversation.messages.filter(inbound=False).order_by("-sent_at").first()
    conversation.last_message_at = last.sent_at
    conversation.last_inbound_at = inbound.sent_at if inbound else None
    conversation.last_outbound_at = outbound.sent_at if outbound else None
    conversation.needs_reply = (
        bool(last.inbound) and conversation.status != ConversationStatus.OPTED_OUT
    )
    if new_inbound:
        conversation.unread = True
    conversation.save()


def sync_conversations(client: InstagramClient, account: SocialAccount) -> dict[str, int]:
    threads = new_messages = 0
    for item in client.list_conversations(max_items=CONVERSATION_LIMIT):
        conversation = upsert_conversation(account, item)
        new_inbound = False
        for raw in (item.get("messages") or {}).get("data", []):
            message, created = upsert_message(account, conversation, raw)
            new_messages += int(created)
            new_inbound = new_inbound or (created and message.inbound)
        refresh_conversation_state(conversation, new_inbound)
        threads += 1
    return {"conversations": threads, "new_messages": new_messages}


# --- orchestration ----------------------------------------------------------------------------


def _record(account: SocialAccount, counts: dict[str, int], error: str) -> None:
    account.last_synced_at = timezone.now()
    account.last_error = error[:300]
    account.counts = counts
    account.save(update_fields=["last_synced_at", "last_error", "counts", "updated_at"])


def _subscribe(client: InstagramClient, account: SocialAccount) -> None:
    if account.webhooks_subscribed:
        return
    try:
        client.subscribe_webhooks(WEBHOOK_FIELDS)
    except AuthExpiredError, RateLimitedError:
        raise
    except DomainError:
        return  # the app may not have webhooks configured yet; the settings screen shows this
    account.webhooks_subscribed = True
    account.save(update_fields=["webhooks_subscribed", "updated_at"])


Step = Callable[[], dict[str, int]]


def sync_connection(connection: Connection, *, inbox_only: bool = False) -> dict[str, int]:
    """Refresh one connection. ``inbox_only`` skips insights and is cheap enough to run often."""
    now = timezone.now()
    counts: dict[str, int] = {}
    account: SocialAccount | None = None
    try:
        with factory.open_client(connection) as client:
            account = upsert_account(connection, client.me())
            counts["media"] = sync_media(client, account, now)
            counts.update(sync_comments(client, account, now))
            counts.update(sync_conversations(client, account))
            if not inbox_only:
                counts["metrics"] = sync_media_metrics(client, account, now)
                counts["insights"] = int(sync_account_insights(client, account, now))
                counts["audience"] = int(sync_audience(client, account, now))
                _subscribe(client, account)
    except DomainError as exc:
        if account is not None:
            _record(account, counts, exc.code.value)
        raise
    _record(account, counts, "")
    return counts


def account_for(workspace_id: uuid.UUID, connection_id: uuid.UUID) -> SocialAccount | None:
    return SocialAccount.objects.filter(
        workspace_id=workspace_id, connection_id=connection_id
    ).first()


def days_ago(days: int) -> date:
    return (timezone.now() - timedelta(days=days)).date()
