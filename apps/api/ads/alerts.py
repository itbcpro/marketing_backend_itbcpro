"""Daily checks that turn stored account data into alerts people can act on.

An alert is opened the first time a condition is found and stays open while it keeps being found;
when it stops, it is resolved. Nothing here calls a platform.
"""

from datetime import timedelta
from typing import Any

from django.utils import timezone

from ads import providers
from ads.models import AdAlert, AdEntity, AdLevel, AdSyncState
from ads.openai.stats import totals
from connections.models import Connection
from core.logging import get_logger

log = get_logger(__name__)

LOST_BUDGET_ALERT = 0.3
STALE_AFTER = timedelta(days=3)
MIN_AGE_DAYS = 7
DROP_RATIO = 0.5
MIN_PRIOR_CONVERSIONS = 5
SILENT_CLICKS = 100
Found = tuple[str, AdEntity | None, str, dict[str, Any]]


def _entity_alerts(entities: list[AdEntity]) -> list[Found]:
    found: list[Found] = []
    today = timezone.now().date()
    for entity in entities:
        if entity.review_status == "rejected":
            found.append(("ad_rejected", entity, "high", {}))
        issues = [i.get("code", "") for i in entity.data.get("serving_issues") or []]
        if issues and entity.status == "active":
            found.append(
                ("limited_serving", entity, "medium", {"codes": ", ".join(sorted(set(issues)))})
            )
        share = entity.data.get("impression_share") or {}
        if (
            entity.level == AdLevel.CAMPAIGN
            and (share.get("lost_budget") or 0) >= LOST_BUDGET_ALERT
        ):
            found.append(
                ("budget_limited", entity, "medium", {"lost": round(share["lost_budget"] * 100)})
            )
        if entity.level in {AdLevel.AD, AdLevel.ASSET_GROUP} and entity.status == "active":
            age = (timezone.now() - entity.first_seen_at).days
            week = totals([entity.pk], today - timedelta(days=7), today)
            if age >= MIN_AGE_DAYS and not week["impressions"]:
                found.append(("no_delivery", entity, "high", {}))
    return found


def _account_alerts(connection: Connection, leaves: list[AdEntity]) -> list[Found]:
    found: list[Found] = []
    state = AdSyncState.objects.filter(connection=connection).first()
    if state is None or state.last_synced_at is None:
        return found
    if state.last_error:
        found.append(("sync_failed", None, "high", {}))
    if timezone.now() - state.last_synced_at > STALE_AFTER:
        found.append(
            ("sync_stale", None, "low", {"days": (timezone.now() - state.last_synced_at).days})
        )
    today = timezone.now().date()
    ids = [e.pk for e in leaves]
    recent = totals(ids, today - timedelta(days=7), today)
    prior = totals(ids, today - timedelta(days=14), today - timedelta(days=8))
    if (
        prior["conversions"] >= MIN_PRIOR_CONVERSIONS
        and recent["conversions"] < prior["conversions"] * DROP_RATIO
    ):
        found.append(
            (
                "conversion_drop",
                None,
                "high",
                {"before": prior["conversions"], "after": recent["conversions"]},
            )
        )
    month = totals(ids, today - timedelta(days=30), today)
    if month["clicks"] >= SILENT_CLICKS and not month["conversions"] and prior["conversions"] == 0:
        earlier = totals(ids, today - timedelta(days=30), today - timedelta(days=15))
        if earlier["conversions"] > 0:
            found.append(("tracking_silent", None, "high", {"clicks": int(month["clicks"])}))
    return found


def evaluate(connection: Connection) -> dict[str, int]:
    """Open new alerts, keep current ones and resolve those that stopped. Returns counts."""
    entities = list(AdEntity.objects.filter(connection=connection, missing_since__isnull=True))
    leaves = [e for e in entities if e.level in {AdLevel.AD, AdLevel.ASSET_GROUP}]
    found = _entity_alerts(entities) + _account_alerts(connection, leaves)
    now = timezone.now()
    keys = set()
    opened = 0
    for code, entity, severity, params in found:
        keys.add((code, entity.pk if entity else None))
        alert = AdAlert.objects.filter(
            connection=connection, code=code, entity=entity, resolved_at__isnull=True
        ).first()
        if alert is None:
            AdAlert.objects.create(
                workspace_id=connection.workspace_id,
                connection=connection,
                entity=entity,
                code=code,
                severity=severity,
                params=params,
                first_seen_at=now,
                last_seen_at=now,
            )
            opened += 1
        else:
            alert.severity, alert.params, alert.last_seen_at = severity, params, now
            alert.save(update_fields=["severity", "params", "last_seen_at", "updated_at"])
    resolved = 0
    for alert in AdAlert.objects.filter(connection=connection, resolved_at__isnull=True):
        if (alert.code, alert.entity_id) not in keys:
            alert.resolved_at = now
            alert.save(update_fields=["resolved_at", "updated_at"])
            resolved += 1
    return {"opened": opened, "resolved": resolved, "open": len(keys)}


def evaluate_all() -> int:
    """Run the checks for every connected ad account."""
    done = 0
    for connection in Connection.objects.filter(provider__in=list(providers.PROVIDERS)).exclude(
        status="revoked"
    ):
        evaluate(connection)
        done += 1
    return done
