"""Rule-based audit of a Google Ads account's conversion tracking (from the stored copy)."""

from datetime import timedelta
from typing import Any

from django.utils import timezone

from ads.models import AdEntity, AdLevel, AdSyncState
from ads.openai.stats import totals
from connections.models import Connection

LEAD_CATEGORIES = {
    "SUBMIT_LEAD_FORM",
    "CONTACT",
    "REQUEST_QUOTE",
    "BOOK_APPOINTMENT",
    "SIGNUP",
    "LEAD",
}
SALE_CATEGORIES = {"PURCHASE"}
LAST_CLICK = "GOOGLE_ADS_LAST_CLICK"
SILENT_CLICKS = 100


def _finding(code: str, level: str, **params: Any) -> dict[str, Any]:
    return {"code": code, "level": level, "params": params}


def _action_findings(enabled: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    primary = [a for a in enabled if a.get("primary")]
    if not primary:
        findings.append(_finding("no_primary", "warn"))
    names: dict[str, list[str]] = {}
    for action in primary:
        names.setdefault(str(action.get("category")), []).append(str(action.get("name")))
    for category, listed in names.items():
        if len(listed) > 1:
            findings.append(
                _finding(
                    "duplicate_primary",
                    "warn",
                    category=category.lower(),
                    names=", ".join(listed),
                )
            )
    for action in enabled:
        category = str(action.get("category"))
        if category in LEAD_CATEGORIES and action.get("counting") == "MANY_PER_CLICK":
            findings.append(_finding("many_per_click_leads", "warn", name=action.get("name")))
        if category in SALE_CATEGORIES and not action.get("default_value"):
            findings.append(_finding("no_value", "info", name=action.get("name")))
        if action.get("attribution") == LAST_CLICK:
            findings.append(_finding("last_click", "info", name=action.get("name")))
    return findings


def audit(connection: Connection) -> dict[str, Any]:
    state = AdSyncState.objects.filter(connection=connection).first()
    actions = list((state.account if state else {}).get("conversion_actions") or [])
    enabled = [a for a in actions if a.get("status") == "enabled"]
    findings: list[dict[str, Any]] = []
    if not enabled:
        findings.append(_finding("no_conversion_actions", "fail"))
    else:
        findings += _action_findings(enabled)
    today = timezone.now().date()
    ads = list(
        AdEntity.objects.filter(
            connection=connection,
            level__in=[AdLevel.AD, AdLevel.ASSET_GROUP],
            missing_since__isnull=True,
        ).values_list("pk", flat=True)
    )
    month = totals(ads, today - timedelta(days=30), today)
    if month["clicks"] >= SILENT_CLICKS and not month["conversions"] and enabled:
        findings.append(_finding("tracking_silent", "fail", clicks=int(month["clicks"])))
    return {
        "actions": actions,
        "findings": findings,
        "totals_30d": month,
    }
