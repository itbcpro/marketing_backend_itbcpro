"""Statistics of an account's own results, computed before any AI writes about them.

The AI is given these numbers and may only quote them. Every comparison carries its sample size
``n`` and a ``reliable`` flag (five or more posts), so advice built on three posts is labelled as a
hypothesis. Medians are used instead of means because one viral post would distort a mean.
"""

import re
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from social.models import (
    AccountInsight,
    CommentStatus,
    ConversationStatus,
    SocialAccount,
    SocialComment,
    SocialConversation,
    SocialMedia,
    SocialMessage,
)

MIN_RELIABLE = 5
TOP_COUNT = 5
HASHTAG = re.compile(r"(?<!\w)#\w+")
METRIC_KEYS = ("reach", "views", "interactions", "saves", "shares", "engagement_rate")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HOUR_BUCKETS = (
    ("night", 23, 5),
    ("morning", 5, 11),
    ("midday", 11, 15),
    ("afternoon", 15, 18),
    ("evening", 18, 23),
)
CAPTION_BUCKETS = (("short", 0, 80), ("medium", 80, 300), ("long", 300, 10_000))
HASHTAG_BUCKETS = (("none", 0, 1), ("one_to_five", 1, 6), ("six_or_more", 6, 100))
FORMAT_ORDER = ("image", "carousel", "reel", "video")
TIME_ORDER = ("morning", "midday", "afternoon", "evening", "night")
CAPTION_ORDER = ("short", "medium", "long")
HASHTAG_ORDER = ("none", "one_to_five", "six_or_more")
MIN_REACH_FOR_RATE = 20
DAYS_PER_WEEK = 7
COMPARE_GAP_DAYS = 20
MINUTES = 60


def _median(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 4) if clean else None


def _metric(media: SocialMedia, key: str) -> float | None:
    value = (media.metrics or {}).get(key)
    return float(value) if isinstance(value, int | float) else None


def measured(rows: Iterable[SocialMedia]) -> list[SocialMedia]:
    return [m for m in rows if m.metrics_synced_at is not None and m.metrics]


def summarise(rows: list[SocialMedia]) -> dict[str, Any]:
    summary: dict[str, Any] = {"n": len(rows), "reliable": len(rows) >= MIN_RELIABLE}
    for key in METRIC_KEYS:
        summary[f"median_{key}"] = _median(_metric(m, key) for m in rows)
    watch = _median(_metric(m, "avg_watch_seconds") for m in rows)
    if watch is not None:
        summary["median_avg_watch_seconds"] = watch
    return summary


def group_by(
    rows: list[SocialMedia],
    label_of: Callable[[SocialMedia], str],
    order: tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    """Statistics per label; labels listed in ``order`` come first, in that order."""
    groups: dict[str, list[SocialMedia]] = defaultdict(list)
    for media in rows:
        groups[label_of(media)].append(media)

    def position(label: str) -> tuple[int, str]:
        return (order.index(label) if label in order else len(order), label)

    return {label: summarise(groups[label]) for label in sorted(groups, key=position)}


def _bucket(value: int, buckets: tuple[tuple[str, int, int], ...]) -> str:
    for label, low, high in buckets:
        if low <= value < high:
            return label
    return buckets[-1][0]


def _hour_bucket(moment: datetime) -> str:
    hour = moment.hour
    for label, start, end in HOUR_BUCKETS:
        if (start > end and (hour >= start or hour < end)) or start <= hour < end:
            return label
    return "evening"


def _lift(group: dict[str, Any], overall: float | None, key: str = "median_reach") -> float | None:
    value = group.get(key)
    if not group.get("reliable") or not value or not overall:
        return None
    return round(float(value) / overall, 2)


def _with_lift(
    groups: dict[str, dict[str, Any]], overall: float | None
) -> dict[str, dict[str, Any]]:
    return {k: {**v, "reach_lift_vs_all": _lift(v, overall)} for k, v in groups.items()}


def _post_view(media: SocialMedia) -> dict[str, Any]:
    return {
        "id": str(media.pk),
        "kind": media.kind,
        "posted_at": media.posted_at.isoformat(),
        "caption_start": media.caption[:140],
        "metrics": media.metrics,
        "comments": media.comments_count,
    }


def _ranked(rows: list[SocialMedia]) -> tuple[list[SocialMedia], list[SocialMedia]]:
    eligible = [
        m
        for m in rows
        if (_metric(m, "reach") or 0) >= MIN_REACH_FOR_RATE
        and _metric(m, "engagement_rate") is not None
    ]
    ordered = sorted(eligible, key=lambda m: _metric(m, "engagement_rate") or 0, reverse=True)
    return ordered[:TOP_COUNT], ordered[-TOP_COUNT:][::-1] if len(ordered) > TOP_COUNT else []


def cadence(rows: list[SocialMedia], period_days: int) -> dict[str, Any]:
    times = sorted(m.posted_at for m in rows)
    gaps = [(b - a).days for a, b in pairwise(times)]
    return {
        "posts": len(rows),
        "posts_per_week": round(len(rows) / (period_days / DAYS_PER_WEEK), 1),
        "longest_gap_days": max(gaps) if gaps else None,
        "days_since_last_post": (timezone.now() - times[-1]).days if times else None,
    }


def _insight_trend(account: SocialAccount) -> dict[str, Any]:
    rows = list(AccountInsight.objects.filter(account=account).order_by("-day")[:90])
    if not rows:
        return {"available": False}
    latest = rows[0]
    earlier = next((r for r in rows if (latest.day - r.day).days >= COMPARE_GAP_DAYS), None)
    trend: dict[str, Any] = {
        "available": True,
        "as_of": str(latest.day),
        "window_days": latest.window_days,
        "totals": latest.totals,
        "followers": latest.followers_count,
    }
    if earlier is not None:
        trend["previous"] = {"as_of": str(earlier.day), "totals": earlier.totals}
        trend["followers_change"] = latest.followers_count - earlier.followers_count
        trend["change_pct"] = {
            key: round((latest.totals[key] - earlier.totals[key]) / earlier.totals[key] * 100, 1)
            for key in latest.totals
            if earlier.totals.get(key)
        }
    return trend


def _top(rows: list[dict[str, Any]], count: int = 3) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: float(r.get("value") or 0), reverse=True)[:count]


def audience_summary(account: SocialAccount) -> dict[str, Any]:
    audience = account.audience or {}
    followers = audience.get("followers") or {}
    engaged = audience.get("engaged") or {}
    summary: dict[str, Any] = {
        "available": any(followers.values()),
        "followers": {k: _top(v) for k, v in followers.items() if v},
        "engaged": {k: _top(v) for k, v in engaged.items() if v},
    }
    hours = audience.get("online_hours") or {}
    if hours:
        ranked = sorted(hours.items(), key=lambda kv: float(kv[1] or 0), reverse=True)[:3]
        summary["peak_online_hours"] = [int(h) for h, _ in ranked]
    return summary


def response_minutes(account: SocialAccount, since: datetime) -> float | None:
    """Median minutes between a person's message and our next reply (first replies only)."""
    waits: list[float] = []
    threads = SocialConversation.objects.filter(account=account, last_message_at__gte=since)
    for conversation in threads[:100]:
        previous_inbound: SocialMessage | None = None
        for message in conversation.messages.filter(sent_at__gte=since).order_by("sent_at"):
            if message.inbound and previous_inbound is None:
                previous_inbound = message
            elif not message.inbound and previous_inbound is not None:
                waits.append((message.sent_at - previous_inbound.sent_at).total_seconds() / MINUTES)
                previous_inbound = None
    return _median(waits)


def community_summary(account: SocialAccount, days: int = 30) -> dict[str, Any]:
    since = timezone.now() - timedelta(days=days)
    comments = SocialComment.objects.filter(account=account, from_owner=False, posted_at__gte=since)
    categories: dict[str, int] = defaultdict(int)
    sentiments: dict[str, int] = defaultdict(int)
    for category, sentiment in comments.values_list("category", "sentiment"):
        categories[category or "unclassified"] += 1
        sentiments[sentiment or "unclassified"] += 1
    conversations = SocialConversation.objects.filter(account=account)
    return {
        "window_days": days,
        "comments": comments.count(),
        "unanswered_comments": comments.filter(
            status__in=[CommentStatus.NEW, CommentStatus.DRAFT_READY, CommentStatus.ESCALATED]
        ).count(),
        "comment_categories": dict(categories),
        "comment_sentiment": dict(sentiments),
        "open_conversations": conversations.exclude(
            status__in=[ConversationStatus.CLOSED, ConversationStatus.OPTED_OUT]
        ).count(),
        "conversations_waiting_for_reply": conversations.filter(needs_reply=True).count(),
        "conversations_with_a_person": conversations.filter(
            status=ConversationStatus.HANDOFF
        ).count(),
        "median_first_response_minutes": response_minutes(account, since),
    }


def compute_stats(account: SocialAccount, period_days: int = 90) -> dict[str, Any]:
    now = timezone.now()
    since = now - timedelta(days=period_days)
    zone = ZoneInfo(account.workspace.time_zone)
    posts = list(
        SocialMedia.objects.filter(
            account=account, posted_at__gte=since, removed_at__isnull=True
        ).exclude(kind="story")
    )
    rows = measured(posts)
    overall = summarise(rows)
    reach = overall.get("median_reach")
    best, worst = _ranked(rows)
    return {
        "period_days": period_days,
        "timezone_note": f"weekdays and hours are in {zone.key}",
        "account": {
            "username": account.username,
            "type": account.account_type,
            "followers": account.followers_count,
            "following": account.follows_count,
            "total_posts": account.media_count,
        },
        "data": {"posts_in_period": len(posts), "posts_with_metrics": len(rows)},
        "cadence": cadence(posts, period_days),
        "overall": overall,
        "by_format": _with_lift(group_by(rows, lambda m: m.kind, FORMAT_ORDER), reach),
        "by_weekday": _with_lift(
            group_by(rows, lambda m: WEEKDAYS[m.posted_at.astimezone(zone).weekday()], WEEKDAYS),
            reach,
        ),
        "by_time_of_day": _with_lift(
            group_by(rows, lambda m: _hour_bucket(m.posted_at.astimezone(zone)), TIME_ORDER), reach
        ),
        "by_caption_length": _with_lift(
            group_by(rows, lambda m: _bucket(len(m.caption), CAPTION_BUCKETS), CAPTION_ORDER), reach
        ),
        "by_hashtag_count": _with_lift(
            group_by(
                rows,
                lambda m: _bucket(len(HASHTAG.findall(m.caption)), HASHTAG_BUCKETS),
                HASHTAG_ORDER,
            ),
            reach,
        ),
        "best_posts": [_post_view(m) for m in best],
        "weakest_posts": [_post_view(m) for m in worst],
        "account_insights": _insight_trend(account),
        "audience": audience_summary(account),
        "community": community_summary(account),
    }


def baseline_for(media: SocialMedia, period_days: int = 90) -> dict[str, Any]:
    """The account's own medians for posts of the same format, and where this post ranks."""
    since = timezone.now() - timedelta(days=period_days)
    peers = measured(
        SocialMedia.objects.filter(
            account=media.account, kind=media.kind, posted_at__gte=since, removed_at__isnull=True
        ).exclude(pk=media.pk)
    )
    summary = summarise(peers)
    ranks: dict[str, float | None] = {}
    for key in ("reach", "interactions", "saves", "shares", "engagement_rate"):
        own = _metric(media, key)
        values = [v for v in (_metric(p, key) for p in peers) if v is not None]
        ranks[key] = (
            round(sum(1 for v in values if v <= own) / len(values), 2)
            if own is not None and values
            else None
        )
    return {"same_format": summary, "percentile_among_same_format": ranks, "format": media.kind}


def media_view(media: SocialMedia) -> dict[str, Any]:
    snapshots = [
        {"day": str(s.day), "age_hours": s.age_hours, "metrics": s.metrics}
        for s in media.snapshots.order_by("day")[:20]
    ]
    return {
        "id": str(media.pk),
        "kind": media.kind,
        "posted_at": media.posted_at.isoformat(),
        "age_hours": int((timezone.now() - media.posted_at).total_seconds() // 3600),
        "caption": media.caption,
        "likes": media.like_count,
        "comments": media.comments_count,
        "metrics": media.metrics,
        "metrics_over_time": snapshots,
    }
