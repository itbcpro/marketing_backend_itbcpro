"""Instant, rule-based checks for Meta Ads entities (from the stored copy, no AI)."""

from typing import Any

from ads.models import AdEntity, AdLevel
from ads.openai import checks as shared
from ads.openai.checks import Check, _check

HEADLINE_CHARS = 40
PRIMARY_TEXT_CHARS = 125
CONVERSION_OBJECTIVES = {"OUTCOME_SALES", "OUTCOME_LEADS", "OUTCOME_APP_PROMOTION"}
LEARNING_LIMITED = {"fail", "learning_limited"}


def platform_view(entity: AdEntity) -> dict[str, Any]:
    """What Meta itself reports about this item and the ones above it."""
    view = shared.platform_view(entity)
    group = shared._group_of(entity)
    view["learning_stage"] = group.data.get("learning_stage", "") if group else ""
    view["effective_status"] = entity.data.get("effective_status", "")
    return view


def _ad(entity: AdEntity) -> list[Check]:
    creative = entity.data.get("creative") or {}
    checks: list[Check] = []
    title, body = str(creative.get("title") or ""), str(creative.get("body") or "")
    if len(title) > HEADLINE_CHARS:
        checks.append(_check("headline_long", "info", count=len(title), target=HEADLINE_CHARS))
    if not body:
        checks.append(_check("no_primary_text", "warn"))
    elif len(body) > PRIMARY_TEXT_CHARS:
        checks.append(
            _check("primary_text_long", "info", count=len(body), target=PRIMARY_TEXT_CHARS)
        )
    if not creative.get("call_to_action"):
        checks.append(_check("no_call_to_action", "warn"))
    if not creative.get("image_url"):
        checks.append(_check("no_visual", "warn"))
    if creative.get("target_url") and not creative.get("url_tags"):
        checks.append(_check("no_url_tags", "info"))
    return checks


def _ad_group(entity: AdEntity) -> list[Check]:
    checks: list[Check] = []
    if str(entity.data.get("learning_stage", "")) in LEARNING_LIMITED:
        checks.append(_check("learning_limited", "warn"))
    if entity.children.filter(level=AdLevel.AD, missing_since__isnull=True).count() == 1:
        checks.append(_check("single_ad", "info"))
    return checks


def _campaign(entity: AdEntity, account: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    if entity.data.get("objective") in CONVERSION_OBJECTIVES and not (account.get("pixels") or []):
        checks.append(_check("no_pixel", "warn"))
    if entity.data.get("special_ad_categories"):
        checks.append(_check("special_category", "info"))
    cap = account.get("spend_cap_micros")
    spent = account.get("amount_spent_micros")
    if cap and spent and spent >= cap * 0.9:
        checks.append(_check("spend_cap_near", "warn"))
    return checks


def run_checks(entity: AdEntity, account: dict[str, Any]) -> list[Check]:
    checks = shared._delivery_checks(platform_view(entity))
    if entity.level == AdLevel.AD:
        checks += shared._link_checks(entity, account) + _ad(entity)
    elif entity.level == AdLevel.AD_GROUP:
        checks += _ad_group(entity)
    elif entity.level == AdLevel.CAMPAIGN:
        checks += _campaign(entity, account)
    return [*checks, *shared._result_checks(entity)]
