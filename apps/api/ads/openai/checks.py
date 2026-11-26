"""Instant, rule-based checks of one ad from the stored copy (no AI, no platform call).

Each check is a code plus parameters; the screen turns codes into sentences. Platform diagnostics
(review state, serving issues, bid guidance) come from the last sync.
"""

from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from django.utils import timezone

from ads.models import AdEntity, AdLevel
from ads.openai.stats import ad_ids, totals

HINTS_MIN = 8
HINTS_MAX = 20
ENOUGH_IMPRESSIONS = 1000
THIN_IMPRESSIONS = 100
LOW_CTR = 0.005
BELOW_AVERAGE_RATIO = 0.5
MIN_AGE_DAYS = 7
FRAGMENT_STARTS = ("or ", "and ", "also ", "but ", "so ")

Check = dict[str, Any]


def _check(code: str, level: str, **params: Any) -> Check:
    return {"code": code, "level": level, "params": params}


def _group_of(entity: AdEntity) -> AdEntity | None:
    if entity.level == AdLevel.AD_GROUP:
        return entity
    return entity.parent if entity.level == AdLevel.AD else None


def _campaign_of(entity: AdEntity) -> AdEntity | None:
    if entity.level == AdLevel.CAMPAIGN:
        return entity
    group = _group_of(entity)
    if group is not None:
        return group.parent
    return None


def platform_view(entity: AdEntity) -> dict[str, Any]:
    """What the platform itself says about this ad, its ad group and its campaign."""
    group = _group_of(entity)
    campaign = _campaign_of(entity)
    issues = [
        {"level": level, "code": str(issue.get("code", "unknown"))}
        for level, node in (
            ("ad", entity if entity.level == AdLevel.AD else None),
            ("ad_group", group),
            ("campaign", campaign),
        )
        if node
        for issue in (node.data.get("serving_issues") or [])
    ]
    review = entity.data.get("review") or {}
    bidding = (group.data.get("bidding_config") if group else None) or {}
    return {
        "review_status": entity.review_status or review.get("status", ""),
        "review": review,
        "appeal": entity.data.get("appeal"),
        "serving_issues": issues,
        "bid_too_low": bool(group.data.get("bid_too_low")) if group else False,
        "max_bid_micros": bidding.get("max_bid_micros"),
        "phone_verification_required": bool(
            entity.data.get("advertiser_phone_verification_required")
        ),
        "geographic_targeting_blocked": bool(entity.data.get("geographic_targeting_blocked")),
        "statuses": {
            "ad": entity.status if entity.level == AdLevel.AD else "",
            "ad_group": group.status if group else "",
            "campaign": campaign.status if campaign else "",
        },
    }


def _delivery_checks(platform: dict[str, Any]) -> list[Check]:
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
    elif not not_running and review == "approved":
        checks.append(_check("delivery_clear", "ok"))
    if platform["bid_too_low"]:
        checks.append(_check("bid_too_low", "warn"))
    if platform["phone_verification_required"]:
        checks.append(_check("phone_verification", "fail"))
    return checks


def _link_checks(entity: AdEntity, account: dict[str, Any]) -> list[Check]:
    url = str((entity.data.get("creative") or {}).get("target_url") or "")
    if not url:
        return []
    if not url.startswith("https://"):
        return [_check("link_not_https", "fail")]
    site = urlparse(str(account.get("url") or "")).hostname
    host = urlparse(url).hostname or ""
    if site and host and not host.removeprefix("www.").endswith(site.removeprefix("www.")):
        return [_check("link_off_site", "warn", host=host, site=site)]
    return [_check("link_ok", "ok")]


def _hint_checks(entity: AdEntity) -> list[Check]:
    group = _group_of(entity)
    hints = list((group.data.get("context_hints") if group else None) or [])
    if not hints:
        return [_check("hints_none", "fail")]
    checks: list[Check] = []
    if len(hints) < HINTS_MIN:
        checks.append(_check("hints_few", "warn", count=len(hints), minimum=HINTS_MIN))
    elif len(hints) > HINTS_MAX:
        checks.append(_check("hints_many", "warn", count=len(hints), maximum=HINTS_MAX))
    else:
        checks.append(_check("hints_ok", "ok", count=len(hints)))
    fragments = [h for h in hints if str(h).startswith(FRAGMENT_STARTS)]
    if fragments:
        checks.append(_check("hints_fragmented", "warn", count=len(fragments)))
    siblings = group.children.filter(missing_since__isnull=True).count() if group else 0
    if siblings == 1:
        checks.append(_check("single_ad", "info"))
    return checks


def _result_checks(entity: AdEntity) -> list[Check]:
    today = timezone.now().date()
    checks: list[Check] = []
    ids = ad_ids(entity)
    week = totals(ids, today - timedelta(days=7), today)
    month = totals(ids, today - timedelta(days=30), today)
    age_days = (timezone.now() - entity.first_seen_at).days
    if entity.status == "active" and not week["impressions"] and age_days >= MIN_AGE_DAYS:
        checks.append(_check("no_delivery", "fail"))
    if month["impressions"] < THIN_IMPRESSIONS:
        return [*checks, _check("thin_data", "info", impressions=int(month["impressions"]))]
    if month["impressions"] < ENOUGH_IMPRESSIONS:
        return checks
    ctr = month["ctr"]
    if ctr < LOW_CTR:
        checks.append(_check("low_ctr", "warn", ctr=round(ctr * 100, 2)))
    account_ids = list(
        AdEntity.objects.filter(
            connection=entity.connection_id, level=AdLevel.AD, missing_since__isnull=True
        ).values_list("pk", flat=True)
    )
    average = totals(account_ids, today - timedelta(days=30), today)["ctr"]
    if average and ctr < average * BELOW_AVERAGE_RATIO:
        checks.append(
            _check(
                "below_average_ctr",
                "warn",
                ctr=round(ctr * 100, 2),
                average=round(average * 100, 2),
            )
        )
    return checks


def run_checks(entity: AdEntity, account: dict[str, Any]) -> list[Check]:
    checks = _delivery_checks(platform_view(entity))
    if entity.level == AdLevel.AD:
        checks += _link_checks(entity, account)
    if entity.level != AdLevel.CAMPAIGN:
        checks += _hint_checks(entity)
    return [*checks, *_result_checks(entity)]
