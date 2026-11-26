"""Keep a local copy of an OpenAI Ads account: structure, daily numbers and detected changes.

Screens read this copy, so opening a page never calls the platform. Sync is triggered by a button
or by the daily schedule; each run also records what changed since the previous run.
"""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from actions.models import ActionStatus, ProposedAction
from ads.models import AdEntity, AdLevel, AdMetricDaily, AdSyncState
from ads.openai import actions
from ads.openai.actions import PREFIX
from ads.syncing import upsert_entity
from connections.models import Connection
from core.errors import DomainError
from core.logging import get_logger

log = get_logger(__name__)

MICROS = 1_000_000
FIRST_SYNC_DAYS = 30
REFRESH_DAYS = 7


def _store_metrics(connection: Connection, rows: list[dict[str, Any]]) -> int:
    ads = {
        e.external_id: e for e in AdEntity.objects.filter(connection=connection, level=AdLevel.AD)
    }
    stored = 0
    for row in rows:
        entity = ads.get(str(row.get("ad_id") or ""))
        day = str(row.get("readable_time") or "")[:10]
        if entity is None or not day:
            continue
        AdMetricDaily.objects.update_or_create(
            entity=entity,
            day=date.fromisoformat(day),
            defaults={
                "workspace_id": connection.workspace_id,
                "impressions": int(row.get("impressions") or 0),
                "clicks": int(row.get("clicks") or 0),
                "spend_micros": round(float(row.get("spend") or 0) * MICROS),
            },
        )
        stored += 1
    return stored


def _account_today(account: dict[str, Any]) -> date:
    """Today in the ad account's timezone; the platform rejects ranges that end in its future."""
    try:
        return datetime.now(ZoneInfo(str(account.get("timezone") or "UTC"))).date()
    except ZoneInfoNotFoundError:
        return datetime.now(ZoneInfo("UTC")).date()


def mirror_row(connection: Connection, level: str, row: dict[str, Any]) -> None:
    """Fold a row the platform just returned for one of our own changes into the local copy."""
    entity = AdEntity.objects.filter(
        connection=connection, level=level, external_id=row.get("id", "")
    ).first()
    if entity is None:
        return
    upsert_entity(connection, level, {**entity.data, **row}, entity.parent, timezone.now())


def _created_ads(connection: Connection) -> dict[str, list[str]]:
    """Ads this app created, by ad group id. The platform's list can lag behind or omit new ads."""
    rows = ProposedAction.objects.filter(
        workspace_id=connection.workspace_id,
        kind=f"{PREFIX}ad.create",
        status__in=[ActionStatus.VERIFIED, ActionStatus.SUCCEEDED],
    )
    found: dict[str, list[str]] = {}
    for action in rows:
        payload = action.effective_payload
        if str(payload.get("connection_id")) != str(connection.pk):
            continue
        resource_id = (action.result or {}).get("resource_id")
        if resource_id:
            found.setdefault(str(payload.get("ad_group_id")), []).append(str(resource_id))
    return found


def _group_ads(
    client: Any, group: dict[str, Any], created: dict[str, list[str]]
) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = client.list_resource(
        "ads", parent=("ad_group_id", group["id"]), include=["serving_issues"]
    )
    have = {row["id"] for row in listed}
    for ad_id in created.get(group["id"], []):
        if ad_id in have:
            continue
        try:
            row = client.get_resource("ads", ad_id)
        except DomainError:
            continue  # gone from the platform
        if row.get("status") != "archived":
            listed.append(row)
    return listed


def sync_connection(connection: Connection) -> dict[str, int]:
    """Refresh structure and numbers. Returns counts; raises the platform error on failure."""
    state, _ = AdSyncState.objects.get_or_create(
        connection=connection, defaults={"workspace_id": connection.workspace_id}
    )
    days = FIRST_SYNC_DAYS if state.last_synced_at is None else REFRESH_DAYS
    now = timezone.now()
    try:
        with actions.client_factory(connection) as client:
            account = client.get_account()
            today = _account_today(account)
            campaigns = client.list_resource("campaigns", include=["serving_issues"])
            created = _created_ads(connection)
            tree: list[
                tuple[dict[str, Any], list[tuple[dict[str, Any], list[dict[str, Any]]]]]
            ] = []
            for raw_campaign in campaigns:
                raw_groups = client.list_resource(
                    "ad_groups",
                    parent=("campaign_id", raw_campaign["id"]),
                    include=["serving_issues", "bid_too_low"],
                )
                tree.append(
                    (
                        raw_campaign,
                        [(g, _group_ads(client, g, created)) for g in raw_groups],
                    )
                )
            metric_rows = client.ad_daily_metrics(
                (today - timedelta(days=days)).isoformat(), today.isoformat()
            )
    except Exception as exc:
        state.last_error = str(exc)[:300]
        state.save(update_fields=["last_error", "updated_at"])
        raise

    counts = {"campaigns": 0, "ad_groups": 0, "ads": 0, "metric_days": 0}
    with transaction.atomic():
        seen: set[str] = set()
        for campaign_row, group_rows in tree:
            c_entity = upsert_entity(connection, AdLevel.CAMPAIGN, campaign_row, None, now)
            seen.add(str(c_entity.pk))
            counts["campaigns"] += 1
            for group_row, ad_rows in group_rows:
                g_entity = upsert_entity(connection, AdLevel.AD_GROUP, group_row, c_entity, now)
                seen.add(str(g_entity.pk))
                counts["ad_groups"] += 1
                for ad_row in ad_rows:
                    a_entity = upsert_entity(connection, AdLevel.AD, ad_row, g_entity, now)
                    seen.add(str(a_entity.pk))
                    counts["ads"] += 1
        AdEntity.objects.filter(connection=connection, missing_since__isnull=True).exclude(
            pk__in=seen
        ).update(missing_since=now)
        counts["metric_days"] = _store_metrics(connection, metric_rows)
        state.last_synced_at = now
        state.last_error = ""
        state.counts = counts
        state.account = account
        state.save()
    return counts
