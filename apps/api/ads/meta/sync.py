"""Keep a local copy of a Meta ad account.

Campaigns, ad sets and ads are read from the Marketing API and mapped into the same entity levels
and field names the other ad platforms use (campaign, ad group, ad), so lists, dashboards, AI
reviews and change history work the same way. Delivery numbers are read per ad per day in the
account's time zone. Money is integer micros of the account currency.
"""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from ads.meta import factory, money
from ads.meta.provider import account_info
from ads.models import AdEntity, AdLevel, AdMetricDaily, AdSyncState
from ads.syncing import upsert_entity
from connections.models import Connection
from core.errors import DomainError
from core.logging import get_logger

log = get_logger(__name__)

METRIC_DAYS = 30
STATUS = {"ACTIVE": "active", "PAUSED": "paused", "ARCHIVED": "archived", "DELETED": "archived"}
REVIEW = {
    "ACTIVE": "approved",
    "PENDING_REVIEW": "in_review",
    "PREAPPROVED": "in_review",
    "IN_PROCESS": "in_review",
    "DISAPPROVED": "rejected",
    "WITH_ISSUES": "rejected",
}
CAMPAIGN_FIELDS = (
    "id,name,status,effective_status,objective,daily_budget,lifetime_budget,budget_remaining,"
    "bid_strategy,start_time,stop_time,buying_type,special_ad_categories,issues_info,updated_time"
)
ADSET_FIELDS = (
    "id,name,status,effective_status,campaign_id,daily_budget,lifetime_budget,optimization_goal,"
    "billing_event,bid_strategy,bid_amount,start_time,end_time,targeting,promoted_object,"
    "learning_stage_info,issues_info,updated_time"
)
AD_FIELDS = (
    "id,name,status,effective_status,adset_id,campaign_id,issues_info,ad_review_feedback,"
    "creative{id,name,title,body,object_type,image_url,thumbnail_url,call_to_action_type,"
    "object_story_spec,url_tags,effective_object_story_id},updated_time"
)
PIXEL_FIELDS = "id,name,last_fired_time,is_unavailable"
# Result actions, counted once each by their plain type (the pixel and "omni" variants repeat them).
RESULT_ACTIONS = (
    "purchase",
    "lead",
    "complete_registration",
    "contact",
    "schedule",
    "submit_application",
    "start_trial",
    "subscribe",
    "onsite_conversion.messaging_conversation_started_7d",
)
VALUE_ACTIONS = ("purchase",)


def _micros(minor: Any, currency: str) -> int | None:
    if minor in (None, ""):
        return None
    try:
        return money.from_minor(minor, currency)
    except ValueError:
        return None


def _budget(row: dict[str, Any], currency: str) -> dict[str, int]:
    daily = _micros(row.get("daily_budget"), currency)
    lifetime = _micros(row.get("lifetime_budget"), currency)
    if daily:
        return {"daily_spend_limit_micros": daily}
    if lifetime:
        return {"lifetime_spend_limit_micros": lifetime}
    return {}


def _issues(row: dict[str, Any]) -> list[dict[str, str]]:
    found = [
        {
            "code": f"issue_{i.get('error_code')}",
            "summary": str(i.get("error_summary") or "")[:200],
            "message": str(i.get("error_message") or "")[:400],
        }
        for i in row.get("issues_info") or []
    ]
    feedback = (row.get("ad_review_feedback") or {}).get("global") or {}
    found += [
        {"code": f"policy_{str(reason).lower().replace(' ', '_')[:50]}", "summary": str(text)[:200]}
        for reason, text in feedback.items()
    ]
    return found


def _status(row: dict[str, Any]) -> str:
    configured = str(row.get("status") or "")
    return STATUS.get(configured, configured.lower())


def _campaign(row: dict[str, Any], currency: str) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row.get("name", ""),
        "status": _status(row),
        "effective_status": row.get("effective_status", ""),
        "objective": row.get("objective", ""),
        "bidding_type": str(row.get("bid_strategy") or "").lower(),
        "start_time": row.get("start_time"),
        "end_time": row.get("stop_time"),
        "budget": _budget(row, currency),
        "budget_remaining_micros": _micros(row.get("budget_remaining"), currency),
        "special_ad_categories": row.get("special_ad_categories") or [],
        "serving_issues": _issues(row),
    }


def _ad_group(row: dict[str, Any], currency: str) -> dict[str, Any]:
    bid = _micros(row.get("bid_amount"), currency)
    learning = (row.get("learning_stage_info") or {}).get("status", "")
    return {
        "id": str(row["id"]),
        "name": row.get("name", ""),
        "status": _status(row),
        "effective_status": row.get("effective_status", ""),
        "start_time": row.get("start_time"),
        "end_time": row.get("end_time"),
        "targeting": row.get("targeting") or {},
        "promoted_object": row.get("promoted_object") or {},
        "learning_stage": str(learning).lower(),
        "bidding_config": {
            "bid_strategy": str(row.get("bid_strategy") or "").lower(),
            "optimization_goal": str(row.get("optimization_goal") or "").lower(),
            "billing_event": str(row.get("billing_event") or "").lower(),
            "max_bid_micros": bid,
            **_budget(row, currency),
        },
        "serving_issues": _issues(row),
    }


def _creative(raw: dict[str, Any]) -> dict[str, Any]:
    spec = raw.get("object_story_spec") or {}
    link = spec.get("link_data") or {}
    video = spec.get("video_data") or {}
    title = raw.get("title") or link.get("name") or video.get("title") or ""
    body = raw.get("body") or link.get("message") or video.get("message") or ""
    url = link.get("link") or (video.get("call_to_action") or {}).get("value", {}).get("link") or ""
    return {
        "type": str(raw.get("object_type") or ("video" if video else "image")).lower(),
        "title": title,
        "body": body,
        "target_url": url,
        "description": link.get("description", ""),
        "call_to_action": raw.get("call_to_action_type")
        or (link.get("call_to_action") or {}).get("type", ""),
        "image_url": raw.get("image_url") or raw.get("thumbnail_url") or "",
        "creative_id": str(raw.get("id") or ""),
        "page_id": spec.get("page_id", ""),
        "instagram_actor_id": spec.get("instagram_actor_id", ""),
        "url_tags": raw.get("url_tags", ""),
        "post_id": raw.get("effective_object_story_id", ""),
    }


def _ad(row: dict[str, Any]) -> dict[str, Any]:
    effective = str(row.get("effective_status") or "")
    creative = _creative(row.get("creative") or {})
    return {
        "id": str(row["id"]),
        "name": row.get("name") or creative["title"] or f"Ad {row['id']}",
        "status": _status(row),
        "effective_status": effective,
        "review_status": REVIEW.get(effective, ""),
        "creative": creative,
        "serving_issues": _issues(row),
    }


def results_of(actions: list[dict[str, Any]] | None, wanted: tuple[str, ...]) -> float:
    by_type = {str(a.get("action_type")): float(a.get("value") or 0) for a in actions or []}
    return sum(by_type.get(kind, 0.0) for kind in wanted)


def _account_today(account: dict[str, Any]) -> date:
    try:
        return datetime.now(ZoneInfo(str(account.get("timezone") or "UTC"))).date()
    except ZoneInfoNotFoundError:
        return datetime.now(ZoneInfo("UTC")).date()


def _metric(row: dict[str, Any], currency: str) -> tuple[str, date, dict[str, float]] | None:
    ad_id, day = row.get("ad_id"), row.get("date_start")
    if not ad_id or not day:
        return None
    spend = float(row.get("spend") or 0)
    return (
        str(ad_id),
        date.fromisoformat(day),
        {
            "impressions": int(row.get("impressions") or 0),
            "clicks": int(row.get("clicks") or 0),
            "spend_micros": round(spend * money.MICROS),
            "conversions": results_of(row.get("actions"), RESULT_ACTIONS),
            "conversion_value": results_of(row.get("action_values"), VALUE_ACTIONS),
        },
    )


def _fetch(connection: Connection, since: date, today: date) -> dict[str, Any]:
    """Everything the copy needs, read in one client session."""
    with factory.open_client(connection) as client:
        account_id = connection.external_account_id
        pixels: list[dict[str, Any]] = []
        try:
            pixels = client.list_edge(account_id, "adspixels", PIXEL_FIELDS)
        except DomainError:
            log.warning("meta_ads_pixels_unavailable")  # the pixel list is optional context
        return {
            "campaigns": client.list_edge(account_id, "campaigns", CAMPAIGN_FIELDS),
            "ad_sets": client.list_edge(account_id, "adsets", ADSET_FIELDS),
            "ads": client.list_edge(account_id, "ads", AD_FIELDS, limit=50),
            "pixels": pixels,
            "insights": client.insights(
                account_id, level="ad", date_from=since.isoformat(), date_to=today.isoformat()
            ),
        }


def _structure(
    connection: Connection, fetched: dict[str, Any], currency: str, now: Any
) -> tuple[dict[str, int], set[str]]:
    counts = {"campaigns": 0, "ad_groups": 0, "ads": 0}
    seen: set[str] = set()

    def put(level: str, data: dict[str, Any], parent: AdEntity | None, key: str) -> AdEntity:
        entity = upsert_entity(connection, level, data, parent, now)
        seen.add(str(entity.pk))
        counts[key] += 1
        return entity

    campaigns: dict[str, AdEntity] = {}
    for row in fetched["campaigns"]:
        data = _campaign(row, currency)
        campaigns[data["id"]] = put(AdLevel.CAMPAIGN, data, None, "campaigns")
    groups: dict[str, AdEntity] = {}
    for row in fetched["ad_sets"]:
        parent = campaigns.get(str(row.get("campaign_id")))
        if parent is not None:
            data = _ad_group(row, currency)
            groups[data["id"]] = put(AdLevel.AD_GROUP, data, parent, "ad_groups")
    for row in fetched["ads"]:
        parent = groups.get(str(row.get("adset_id")))
        if parent is not None:
            put(AdLevel.AD, _ad(row), parent, "ads")
    return counts, seen


def _store_metrics(connection: Connection, insights: list[dict[str, Any]], currency: str) -> int:
    ads = {
        e.external_id: e for e in AdEntity.objects.filter(connection=connection, level=AdLevel.AD)
    }
    stored = 0
    for row in insights:
        parsed = _metric(row, currency)
        if parsed is None or parsed[0] not in ads:
            continue
        ad_id, day, values = parsed
        AdMetricDaily.objects.update_or_create(
            entity=ads[ad_id],
            day=day,
            defaults={"workspace_id": connection.workspace_id, **values},
        )
        stored += 1
    return stored


def _account_view(info: dict[str, Any], pixels: list[dict[str, Any]]) -> dict[str, Any]:
    currency = str(info.get("currency_code") or "USD")
    return {
        **{k: v for k, v in info.items() if k not in {"spend_cap", "amount_spent"}},
        "spend_cap_micros": _micros(info.get("spend_cap"), currency) or None,
        "amount_spent_micros": _micros(info.get("amount_spent"), currency),
        "pixels": [
            {
                "id": str(p.get("id")),
                "name": p.get("name", ""),
                "last_fired_time": p.get("last_fired_time"),
                "unavailable": bool(p.get("is_unavailable")),
            }
            for p in pixels
        ],
    }


def sync_connection(connection: Connection) -> dict[str, int]:
    """Refresh structure and daily numbers from Meta. Raises on platform errors."""
    state, _ = AdSyncState.objects.get_or_create(
        connection=connection, defaults={"workspace_id": connection.workspace_id}
    )
    now = timezone.now()
    try:
        info = account_info(connection)
        today = _account_today(info)
        fetched = _fetch(connection, today - timedelta(days=METRIC_DAYS), today)
    except Exception as exc:
        state.last_error = str(exc)[:300]
        state.save(update_fields=["last_error", "updated_at"])
        raise
    currency = str(info.get("currency_code") or "USD")
    with transaction.atomic():
        counts, seen = _structure(connection, fetched, currency, now)
        AdEntity.objects.filter(connection=connection, missing_since__isnull=True).exclude(
            pk__in=seen
        ).update(missing_since=now)
        metric_days = _store_metrics(connection, fetched["insights"], currency)
        state.last_synced_at = now
        state.last_error = ""
        state.counts = {**counts, "metric_days": metric_days}
        state.account = _account_view(info, fetched["pixels"])
        state.save()
    return {**counts, "metric_days": metric_days}


def mirror_patch(
    connection: Connection, level: str, external_id: str, patch: dict[str, Any]
) -> None:
    """Fold a change we just made into the local copy (and the change history)."""
    entity = AdEntity.objects.filter(
        connection=connection, level=level, external_id=external_id
    ).first()
    if entity is None:
        return
    upsert_entity(
        connection,
        level,
        {**entity.data, **patch, "id": external_id},
        entity.parent,
        timezone.now(),
    )
