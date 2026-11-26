"""Instant, rule-based checks for Google Ads entities (from the stored copy, no AI)."""

from datetime import timedelta
from typing import Any

from django.utils import timezone

from ads.models import AdEntity, AdLevel
from ads.openai.checks import Check, _check, _group_of, _result_checks
from ads.openai.stats import ad_ids, totals

RSA_HEADLINES = 15
RSA_DESCRIPTIONS = 4
HEADLINE_CHARS = 30
DESCRIPTION_CHARS = 90
WEAK_STRENGTH = {"POOR", "AVERAGE"}
LOST_BUDGET_WARN = 0.2
LOST_RANK_WARN = 0.4
LOW_QUALITY = 5
ASSET_MINIMUMS = {
    "HEADLINE": 3,
    "LONG_HEADLINE": 1,
    "DESCRIPTION": 2,
    "MARKETING_IMAGE": 1,
    "SQUARE_MARKETING_IMAGE": 1,
    "LOGO": 1,
}
ENOUGH_CLICKS = 100
SERVING_PROBLEMS = {"NOT_ELIGIBLE", "LIMITED"}
BLOCKING = {"campaign_paused", "ad_group_paused", "paused", "removed", "pending"}


def platform_view(entity: AdEntity) -> dict[str, Any]:
    """What Google itself reports about this item and the ones above it."""
    group = _group_of(entity)
    campaign = (
        entity
        if entity.level == AdLevel.CAMPAIGN
        else (
            group.parent
            if group
            else entity.parent
            if entity.level == AdLevel.ASSET_GROUP
            else None
        )
    )
    issues = [
        {"level": level, "code": str(i.get("code", "unknown"))}
        for level, node in (
            (
                "ad" if entity.level == AdLevel.AD else entity.level,
                entity if entity.level not in {AdLevel.CAMPAIGN, AdLevel.AD_GROUP} else None,
            ),
            ("ad_group", group),
            ("campaign", campaign),
        )
        if node
        for i in (node.data.get("serving_issues") or [])
    ]
    return {
        "review_status": entity.review_status,
        "review": {},
        "appeal": None,
        "serving_issues": issues,
        "bid_too_low": False,
        "max_bid_micros": (
            (group.data.get("bidding_config") or {}).get("max_bid_micros") if group else None
        ),
        "phone_verification_required": False,
        "geographic_targeting_blocked": False,
        "ad_strength": entity.data.get("ad_strength", ""),
        "impression_share": (campaign.data.get("impression_share") if campaign else None),
        "statuses": {
            "ad": entity.status if entity.level == AdLevel.AD else "",
            "ad_group": group.status if group else "",
            "campaign": campaign.status if campaign else "",
            "asset_group": entity.status if entity.level == AdLevel.ASSET_GROUP else "",
        },
    }


def _delivery(platform: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    review = platform["review_status"]
    if review == "rejected":
        checks.append(_check("review_rejected", "fail"))
    elif review == "in_review":
        checks.append(_check("review_in_review", "warn"))
    elif review == "approved":
        checks.append(_check("review_approved", "ok"))
    not_running = [lvl for lvl, st in platform["statuses"].items() if st and st != "active"]
    if not_running:
        checks.append(_check("not_active", "warn", levels=", ".join(not_running)))
    if platform["serving_issues"]:
        codes = sorted({i["code"] for i in platform["serving_issues"]})
        checks.append(_check("serving_issues", "fail", codes=", ".join(codes)))
    elif not not_running:
        checks.append(_check("delivery_clear", "ok"))
    return checks


def _rsa(entity: AdEntity) -> list[Check]:
    creative = entity.data.get("creative") or {}
    headlines = list(creative.get("headlines") or [])
    descriptions = list(creative.get("descriptions") or [])
    checks: list[Check] = []
    if len(headlines) < RSA_HEADLINES:
        checks.append(
            _check("rsa_few_headlines", "warn", count=len(headlines), target=RSA_HEADLINES)
        )
    else:
        checks.append(_check("rsa_headlines_ok", "ok", count=len(headlines)))
    if len(descriptions) < RSA_DESCRIPTIONS:
        checks.append(
            _check("rsa_few_descriptions", "warn", count=len(descriptions), target=RSA_DESCRIPTIONS)
        )
    if len({h.lower() for h in headlines}) < len(headlines):
        checks.append(_check("rsa_duplicate_headlines", "warn"))
    if creative.get("pinned_headlines") and len(creative["pinned_headlines"]) > 2:  # noqa: PLR2004
        checks.append(_check("rsa_many_pins", "info", count=len(creative["pinned_headlines"])))
    strength = str(entity.data.get("ad_strength", ""))
    if strength in WEAK_STRENGTH:
        checks.append(_check("ad_strength_low", "warn", strength=strength.lower()))
    elif strength:
        checks.append(_check("ad_strength_ok", "ok", strength=strength.lower()))
    url = str((creative.get("final_urls") or [""])[0])
    if url and not url.startswith("https://"):
        checks.append(_check("link_not_https", "fail"))
    return checks


def _keywords(entity: AdEntity) -> list[Check]:
    group = _group_of(entity)
    if group is None:
        return []
    live = group.children.filter(level=AdLevel.KEYWORD, missing_since__isnull=True)
    positive = [k for k in live if not k.data.get("negative")]
    checks: list[Check] = []
    if not positive:
        return [_check("no_keywords", "fail")]
    low = [k for k in positive if (k.data.get("quality_score") or 10) < LOW_QUALITY]
    if low:
        checks.append(_check("low_quality_keywords", "warn", count=len(low), total=len(positive)))
    if all(k.data.get("match_type") == "broad" for k in positive):
        checks.append(_check("only_broad_match", "warn"))
    if group.children.filter(level=AdLevel.AD, missing_since__isnull=True).count() == 1:
        checks.append(_check("single_ad", "info"))
    return checks


def _campaign(entity: AdEntity, account: dict[str, Any]) -> list[Check]:
    data = entity.data
    checks: list[Check] = []
    share = data.get("impression_share") or {}
    if (share.get("lost_budget") or 0) > LOST_BUDGET_WARN:
        checks.append(_check("budget_limited", "warn", lost=round(share["lost_budget"] * 100)))
    if (share.get("lost_rank") or 0) > LOST_RANK_WARN:
        checks.append(_check("rank_limited", "warn", lost=round(share["lost_rank"] * 100)))
    settings = data.get("network_settings") or {}
    if data.get("channel_type") == "SEARCH" and settings.get("targetContentNetwork"):
        checks.append(_check("display_expansion_on", "warn"))
    if data.get("budget_shared"):
        checks.append(_check("shared_budget", "info"))
    actions = account.get("conversion_actions") or []
    if not any(a.get("primary") and a.get("status") == "enabled" for a in actions):
        checks.append(_check("no_conversion_tracking", "warn"))
    return checks


def _assets(entity: AdEntity) -> list[Check]:
    assets = entity.data.get("assets") or []
    counts: dict[str, int] = {}
    for asset in assets:
        counts[asset["field_type"]] = counts.get(asset["field_type"], 0) + 1
    checks = [
        _check("assets_missing", "fail", field=field.lower(), have=counts.get(field, 0), need=need)
        for field, need in ASSET_MINIMUMS.items()
        if counts.get(field, 0) < need
    ]
    if not counts.get("YOUTUBE_VIDEO"):
        checks.append(_check("no_video", "info"))
    low = [a for a in assets if a.get("label") in SERVING_PROBLEMS]
    if low:
        checks.append(_check("low_assets", "warn", count=len(low)))
    if not checks:
        checks.append(_check("assets_ok", "ok"))
    return checks


def _results(entity: AdEntity) -> list[Check]:
    checks = _result_checks(entity)
    today = timezone.now().date()
    total = totals(ad_ids(entity), today - timedelta(days=30), today)
    if total["clicks"] >= ENOUGH_CLICKS and not total["conversions"]:
        checks.append(_check("no_conversions", "warn", clicks=int(total["clicks"])))
    return checks


def run_checks(entity: AdEntity, account: dict[str, Any]) -> list[Check]:
    checks = _delivery(platform_view(entity))
    if entity.level == AdLevel.AD:
        checks += _rsa(entity) + _keywords(entity)
    elif entity.level == AdLevel.AD_GROUP:
        checks += _keywords(entity)
    elif entity.level == AdLevel.CAMPAIGN:
        checks += _campaign(entity, account)
    elif entity.level == AdLevel.ASSET_GROUP:
        checks += _assets(entity)
    elif entity.level == AdLevel.KEYWORD and (entity.data.get("quality_score") or 10) < LOW_QUALITY:
        checks.append(_check("keyword_low_quality", "warn", score=entity.data.get("quality_score")))
    if entity.level != AdLevel.KEYWORD:
        checks += _results(entity)
    return checks
