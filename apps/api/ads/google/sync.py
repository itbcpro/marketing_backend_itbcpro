"""Keep a local copy of a Google Ads account.

Google's structure and numbers are read with GAQL and mapped into the same entity levels and
field names the OpenAI Ads copy uses, so lists, dashboards, reviews and change history work the
same way. Search campaigns are campaign, ad group, ad (with keywords beside the ads); Performance
Max campaigns are campaign and asset group. Money is integer micros of the account currency.
"""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from ads.google import factory
from ads.google.provider import customer_info
from ads.models import AdEntity, AdLevel, AdMetricDaily, AdSyncState
from ads.syncing import upsert_entity
from connections.models import Connection
from core.logging import get_logger

log = get_logger(__name__)

METRIC_DAYS = 30
MICROS = 1_000_000
STATUS = {"ENABLED": "active", "PAUSED": "paused", "REMOVED": "archived"}
APPROVAL = {
    "APPROVED": "approved",
    "APPROVED_LIMITED": "approved",
    "AREA_OF_INTEREST_ONLY": "approved",
    "DISAPPROVED": "rejected",
    "UNDER_REVIEW": "in_review",
}
LIMITING_APPROVALS = {"APPROVED_LIMITED", "AREA_OF_INTEREST_ONLY"}
HEALTHY_PRIMARY = {"ELIGIBLE", "", "UNKNOWN", "UNSPECIFIED"}

CAMPAIGNS = """
SELECT campaign.id, campaign.name, campaign.status, campaign.primary_status,
  campaign.primary_status_reasons, campaign.advertising_channel_type,
  campaign.bidding_strategy_type, campaign.start_date_time, campaign.end_date_time,
  campaign.campaign_budget, campaign_budget.id, campaign_budget.amount_micros,
  campaign_budget.explicitly_shared, campaign.network_settings.target_google_search,
  campaign.network_settings.target_search_network, campaign.network_settings.target_content_network
FROM campaign WHERE campaign.status != 'REMOVED'
"""
AD_GROUPS = """
SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.type, ad_group.cpc_bid_micros,
  ad_group.primary_status, ad_group.primary_status_reasons, campaign.id
FROM ad_group WHERE ad_group.status != 'REMOVED'
"""
ADS = """
SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.ad.type, ad_group_ad.status,
  ad_group_ad.ad_strength, ad_group_ad.policy_summary.approval_status,
  ad_group_ad.policy_summary.policy_topic_entries, ad_group_ad.ad.final_urls,
  ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions,
  ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2,
  ad_group.id
FROM ad_group_ad WHERE ad_group_ad.status != 'REMOVED'
"""
KEYWORDS = """
SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text,
  ad_group_criterion.keyword.match_type, ad_group_criterion.status, ad_group_criterion.negative,
  ad_group_criterion.cpc_bid_micros, ad_group_criterion.quality_info.quality_score, ad_group.id
FROM ad_group_criterion
WHERE ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status != 'REMOVED'
LIMIT 5000
"""
ASSET_GROUPS = """
SELECT asset_group.id, asset_group.name, asset_group.status, asset_group.final_urls,
  asset_group.primary_status, asset_group.primary_status_reasons, asset_group.ad_strength,
  campaign.id
FROM asset_group WHERE asset_group.status != 'REMOVED'
"""
GROUP_ASSETS = """
SELECT asset_group.id, asset_group_asset.field_type, asset_group_asset.primary_status,
  asset_group_asset.status, asset.id, asset.name, asset.type, asset.text_asset.text,
  asset.image_asset.full_size.url, asset.image_asset.full_size.width_pixels,
  asset.image_asset.full_size.height_pixels, asset.youtube_video_asset.youtube_video_id
FROM asset_group_asset WHERE asset_group_asset.status != 'REMOVED'
"""
IMPRESSION_SHARE = """
SELECT campaign.id, metrics.search_impression_share, metrics.search_budget_lost_impression_share,
  metrics.search_rank_lost_impression_share
FROM campaign
WHERE segments.date DURING LAST_30_DAYS AND campaign.status = 'ENABLED'
  AND campaign.advertising_channel_type = 'SEARCH'
"""
CONVERSION_ACTIONS = """
SELECT conversion_action.id, conversion_action.name, conversion_action.status,
  conversion_action.type, conversion_action.category, conversion_action.primary_for_goal,
  conversion_action.counting_type, conversion_action.value_settings.default_value,
  conversion_action.attribution_model_settings.attribution_model
FROM conversion_action WHERE conversion_action.status != 'REMOVED'
"""
METRICS = (
    "metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions, "
    "metrics.conversions_value"
)


def _get(row: dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = row
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _issues(primary: str, reasons: list[str] | None) -> list[dict[str, str]]:
    if primary in HEALTHY_PRIMARY:
        return []
    return [{"code": str(r).lower()} for r in (reasons or [str(primary)])]


def _campaign(row: dict[str, Any]) -> dict[str, Any]:
    c = row.get("campaign", {})
    amount = _get(row, "campaignBudget.amountMicros")
    return {
        "id": str(c.get("id")),
        "name": c.get("name", ""),
        "status": STATUS.get(c.get("status", ""), str(c.get("status", "")).lower()),
        "channel_type": c.get("advertisingChannelType", ""),
        "bidding_type": str(c.get("biddingStrategyType", "")).lower(),
        "start_time": c.get("startDateTime"),
        "end_time": c.get("endDateTime"),
        "budget": {"daily_spend_limit_micros": int(amount)} if amount else {},
        "budget_resource": c.get("campaignBudget", ""),
        "budget_shared": bool(_get(row, "campaignBudget.explicitlySharedBudget", False))
        or bool(_get(row, "campaignBudget.explicitlyShared", False)),
        "network_settings": c.get("networkSettings", {}),
        "serving_issues": _issues(c.get("primaryStatus", ""), c.get("primaryStatusReasons")),
        "primary_status": c.get("primaryStatus", ""),
    }


def _ad_group(row: dict[str, Any]) -> dict[str, Any]:
    g = row.get("adGroup", {})
    bid = g.get("cpcBidMicros")
    return {
        "id": str(g.get("id")),
        "name": g.get("name", ""),
        "status": STATUS.get(g.get("status", ""), str(g.get("status", "")).lower()),
        "type": g.get("type", ""),
        "bidding_config": {"max_bid_micros": int(bid)} if bid else {},
        "serving_issues": _issues(g.get("primaryStatus", ""), g.get("primaryStatusReasons")),
    }


def _texts(items: list[dict[str, Any]] | None) -> list[str]:
    return [str(i.get("text", "")) for i in items or []]


def _ad(row: dict[str, Any]) -> dict[str, Any]:
    ga = row.get("adGroupAd", {})
    ad = ga.get("ad", {})
    rsa = ad.get("responsiveSearchAd", {})
    summary = ga.get("policySummary", {})
    approval = str(summary.get("approvalStatus", ""))
    headlines = _texts(rsa.get("headlines"))
    descriptions = _texts(rsa.get("descriptions"))
    final_urls = ad.get("finalUrls") or []
    issues = [
        {"code": f"policy_{str(e.get('topic', 'unknown')).lower().replace(' ', '_')}"}
        for e in summary.get("policyTopicEntries") or []
        if approval != "APPROVED"
    ]
    if approval in LIMITING_APPROVALS and not issues:
        issues = [{"code": "policy_limited"}]
    return {
        "id": str(ad.get("id")),
        "name": ad.get("name") or (headlines[0] if headlines else f"Ad {ad.get('id')}"),
        "status": STATUS.get(ga.get("status", ""), str(ga.get("status", "")).lower()),
        "review_status": APPROVAL.get(approval, ""),
        "ad_strength": ga.get("adStrength", ""),
        "creative": {
            "type": str(ad.get("type", "")).lower(),
            "title": headlines[0] if headlines else "",
            "body": descriptions[0] if descriptions else "",
            "target_url": final_urls[0] if final_urls else "",
            "headlines": headlines,
            "descriptions": descriptions,
            "pinned_headlines": {
                str(h.get("text")): h.get("pinnedField")
                for h in rsa.get("headlines") or []
                if h.get("pinnedField")
            },
            "path1": rsa.get("path1", ""),
            "path2": rsa.get("path2", ""),
            "final_urls": final_urls,
        },
        "serving_issues": issues,
    }


def _keyword(row: dict[str, Any]) -> dict[str, Any]:
    crit = row.get("adGroupCriterion", {})
    group = _get(row, "adGroup.id")
    bid = crit.get("cpcBidMicros")
    return {
        "id": f"{group}~{crit.get('criterionId')}",
        "criterion_id": str(crit.get("criterionId")),
        "name": _get(crit, "keyword.text", ""),
        "match_type": str(_get(crit, "keyword.matchType", "")).lower(),
        "status": STATUS.get(crit.get("status", ""), str(crit.get("status", "")).lower()),
        "negative": bool(crit.get("negative")),
        "max_cpc_micros": int(bid) if bid else None,
        "quality_score": _get(crit, "qualityInfo.qualityScore"),
    }


def _asset_group(row: dict[str, Any]) -> dict[str, Any]:
    g = row.get("assetGroup", {})
    return {
        "id": str(g.get("id")),
        "name": g.get("name", ""),
        "status": STATUS.get(g.get("status", ""), str(g.get("status", "")).lower()),
        "final_urls": g.get("finalUrls") or [],
        "ad_strength": g.get("adStrength", ""),
        "serving_issues": _issues(g.get("primaryStatus", ""), g.get("primaryStatusReasons")),
        "assets": [],
        "asset_keys": [],
    }


def _asset(row: dict[str, Any]) -> dict[str, Any]:
    link = row.get("assetGroupAsset", {})
    asset = row.get("asset", {})
    return {
        "field_type": link.get("fieldType", ""),
        "label": link.get("primaryStatus", ""),
        "asset_id": str(asset.get("id")),
        "name": asset.get("name", ""),
        "type": asset.get("type", ""),
        "text": _get(asset, "textAsset.text"),
        "image_url": _get(asset, "imageAsset.fullSize.url"),
        "width": _get(asset, "imageAsset.fullSize.widthPixels"),
        "height": _get(asset, "imageAsset.fullSize.heightPixels"),
        "video_id": _get(asset, "youtubeVideoAsset.youtubeVideoId"),
    }


def _account_today(account: dict[str, Any]) -> date:
    try:
        return datetime.now(ZoneInfo(str(account.get("timezone") or "UTC"))).date()
    except ZoneInfoNotFoundError:
        return datetime.now(ZoneInfo("UTC")).date()


def _metric_rows(
    client: Any, resource: str, id_paths: tuple[str, ...], since: date, until: date
) -> list[tuple[str, date, dict[str, float]]]:
    query = (
        f"SELECT segments.date, {', '.join(id_paths)}, {METRICS} FROM {resource} "
        f"WHERE segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}'"
    )
    out: list[tuple[str, date, dict[str, float]]] = []
    for row in client.search(query):
        parts = [_get(row, _camel(path)) for path in id_paths]
        day = _get(row, "segments.date")
        if any(p is None for p in parts) or not day:
            continue
        identifier = "~".join(str(p) for p in parts)
        m = row.get("metrics", {})
        out.append(
            (
                identifier,
                date.fromisoformat(day),
                {
                    "impressions": int(m.get("impressions") or 0),
                    "clicks": int(m.get("clicks") or 0),
                    "spend_micros": int(m.get("costMicros") or 0),
                    "conversions": float(m.get("conversions") or 0),
                    "conversion_value": float(m.get("conversionsValue") or 0),
                },
            )
        )
    return out


def _camel(path: str) -> str:
    parts = path.split(".")
    return ".".join(p[0] + p.title().replace("_", "")[1:] if "_" in p else p for p in parts)


def _store(
    connection: Connection, level: str, rows: list[tuple[str, date, dict[str, float]]], key: Any
) -> int:
    entities = {
        e.external_id: e for e in AdEntity.objects.filter(connection=connection, level=level)
    }
    stored = 0
    for identifier, day, values in rows:
        entity = entities.get(key(identifier))
        if entity is None:
            continue
        AdMetricDaily.objects.update_or_create(
            entity=entity,
            day=day,
            defaults={"workspace_id": connection.workspace_id, **values},
        )
        stored += 1
    return stored


def _fetch(connection: Connection, since: date, today: date) -> dict[str, list[Any]]:
    """Every query the copy needs, read in one client session."""
    with factory.open_client(connection) as client:
        return {
            "campaigns": client.search(CAMPAIGNS),
            "groups": client.search(AD_GROUPS),
            "ads": client.search(ADS),
            "keywords": client.search(KEYWORDS),
            "asset_groups": client.search(ASSET_GROUPS),
            "group_assets": client.search(GROUP_ASSETS),
            "share": client.search(IMPRESSION_SHARE),
            "conversions": client.search(CONVERSION_ACTIONS),
            "ad_metrics": _metric_rows(client, "ad_group_ad", ("ad_group_ad.ad.id",), since, today),
            "ag_metrics": _metric_rows(client, "asset_group", ("asset_group.id",), since, today),
            "kw_metrics": _metric_rows(
                client,
                "keyword_view",
                ("ad_group.id", "ad_group_criterion.criterion_id"),
                since,
                today,
            ),
        }


def _impression_shares(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    return {
        str(_get(r, "campaign.id")): {
            "share": _float(_get(r, "metrics.searchImpressionShare")),
            "lost_budget": _float(_get(r, "metrics.searchBudgetLostImpressionShare")),
            "lost_rank": _float(_get(r, "metrics.searchRankLostImpressionShare")),
        }
        for r in rows
    }


def _structure(  # noqa: C901, PLR0912 - one linear mapping per level
    connection: Connection, fetched: dict[str, list[Any]], now: Any
) -> tuple[dict[str, int], set[str]]:
    """Write campaigns, ad groups, ads, keywords and asset groups; return counts and ids seen."""
    shares = _impression_shares(fetched["share"])
    assets_by_group: dict[str, list[dict[str, Any]]] = {}
    for row in fetched["group_assets"]:
        assets_by_group.setdefault(str(_get(row, "assetGroup.id")), []).append(_asset(row))
    counts = {"campaigns": 0, "ad_groups": 0, "ads": 0, "asset_groups": 0, "keywords": 0}
    seen: set[str] = set()

    def put(level: str, data: dict[str, Any], parent: AdEntity | None, key: str) -> AdEntity:
        entity = upsert_entity(connection, level, data, parent, now)
        seen.add(str(entity.pk))
        counts[key] += 1
        return entity

    campaigns: dict[str, AdEntity] = {}
    for row in fetched["campaigns"]:
        data = _campaign(row)
        data["impression_share"] = shares.get(data["id"])
        campaigns[data["id"]] = put(AdLevel.CAMPAIGN, data, None, "campaigns")
    groups: dict[str, AdEntity] = {}
    for row in fetched["groups"]:
        parent = campaigns.get(str(_get(row, "campaign.id")))
        if parent is not None:
            data = _ad_group(row)
            groups[data["id"]] = put(AdLevel.AD_GROUP, data, parent, "ad_groups")
    for row in fetched["ads"]:
        parent = groups.get(str(_get(row, "adGroup.id")))
        if parent is not None:
            put(AdLevel.AD, _ad(row), parent, "ads")
    positive: dict[str, int] = {}
    for row in fetched["keywords"]:
        parent = groups.get(str(_get(row, "adGroup.id")))
        if parent is None:
            continue
        data = _keyword(row)
        put(AdLevel.KEYWORD, data, parent, "keywords")
        if not data["negative"]:
            positive[parent.external_id] = positive.get(parent.external_id, 0) + 1
    for external_id, total in positive.items():
        group = groups[external_id]
        if group.data.get("keyword_count") != total:
            group.data = {**group.data, "keyword_count": total}
            group.save(update_fields=["data", "updated_at"])
    for row in fetched["asset_groups"]:
        parent = campaigns.get(str(_get(row, "campaign.id")))
        if parent is None:
            continue
        data = _asset_group(row)
        data["assets"] = assets_by_group.get(data["id"], [])
        data["asset_keys"] = sorted(f"{a['field_type']}:{a['asset_id']}" for a in data["assets"])
        put(AdLevel.ASSET_GROUP, data, parent, "asset_groups")
    return counts, seen


def sync_connection(connection: Connection) -> dict[str, int]:
    """Refresh structure, daily numbers and diagnostics from Google. Raises on platform errors."""
    state, _ = AdSyncState.objects.get_or_create(
        connection=connection, defaults={"workspace_id": connection.workspace_id}
    )
    now = timezone.now()
    try:
        account = customer_info(connection)
        today = _account_today(account)
        fetched = _fetch(connection, today - timedelta(days=METRIC_DAYS), today)
    except Exception as exc:
        state.last_error = str(exc)[:300]
        state.save(update_fields=["last_error", "updated_at"])
        raise

    with transaction.atomic():
        counts, seen = _structure(connection, fetched, now)
        AdEntity.objects.filter(connection=connection, missing_since__isnull=True).exclude(
            pk__in=seen
        ).update(missing_since=now)
        metric_days = sum(
            _store(connection, level, fetched[name], lambda i: i)
            for level, name in (
                (AdLevel.AD, "ad_metrics"),
                (AdLevel.ASSET_GROUP, "ag_metrics"),
                (AdLevel.KEYWORD, "kw_metrics"),
            )
        )
        state.last_synced_at = now
        state.last_error = ""
        state.counts = {**counts, "metric_days": metric_days}
        state.account = {
            **account,
            "conversion_actions": [_conversion_action(r) for r in fetched["conversions"]],
        }
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


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _conversion_action(row: dict[str, Any]) -> dict[str, Any]:
    c = row.get("conversionAction", {})
    return {
        "id": str(c.get("id")),
        "name": c.get("name", ""),
        "status": str(c.get("status", "")).lower(),
        "type": c.get("type", ""),
        "category": c.get("category", ""),
        "primary": bool(c.get("primaryForGoal")),
        "counting": c.get("countingType", ""),
        "default_value": _get(c, "valueSettings.defaultValue"),
        "attribution": _get(c, "attributionModelSettings.attributionModel", ""),
    }
