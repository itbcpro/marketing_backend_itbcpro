"""Aggregated delivery numbers from the stored daily rows."""

import uuid
from datetime import date, timedelta
from typing import Any

from django.db.models import Sum
from django.utils import timezone

from ads.models import AdEntity, AdLevel, AdMetricDaily

MICROS = 1_000_000


def ad_ids(entity: AdEntity) -> list[uuid.UUID]:
    """The ads an entity covers: itself for an ad, all beneath it for a group or campaign."""
    if entity.level in {AdLevel.AD, AdLevel.ASSET_GROUP, AdLevel.KEYWORD}:
        return [entity.pk]
    live = AdEntity.objects.filter(connection=entity.connection_id, missing_since__isnull=True)
    if entity.level == AdLevel.CAMPAIGN:
        ads = live.filter(level=AdLevel.AD, parent__parent=entity)
        groups = live.filter(level=AdLevel.ASSET_GROUP, parent=entity)
        return [*ads.values_list("pk", flat=True), *groups.values_list("pk", flat=True)]
    return list(live.filter(level=AdLevel.AD, parent=entity).values_list("pk", flat=True))


def totals(entity_ids: list[uuid.UUID], since: date, until: date) -> dict[str, float]:
    agg = AdMetricDaily.objects.filter(
        entity_id__in=entity_ids, day__gte=since, day__lte=until
    ).aggregate(
        i=Sum("impressions"),
        c=Sum("clicks"),
        s=Sum("spend_micros"),
        v=Sum("conversions"),
        w=Sum("conversion_value"),
    )
    impressions, clicks = int(agg["i"] or 0), int(agg["c"] or 0)
    spend = int(agg["s"] or 0) / MICROS
    conversions = float(agg["v"] or 0)
    value = float(agg["w"] or 0)
    return {
        "impressions": impressions,
        "clicks": clicks,
        "spend": round(spend, 2),
        "ctr": round(clicks / impressions, 4) if impressions else 0.0,
        "cpc": round(spend / clicks, 2) if clicks else 0.0,
        "conversions": round(conversions, 2),
        "conversion_value": round(value, 2),
        "cpa": round(spend / conversions, 2) if conversions else 0.0,
        "roas": round(value / spend, 2) if spend and value else 0.0,
    }


def series(entity_ids: list[uuid.UUID], days: int) -> list[dict[str, Any]]:
    since = timezone.now().date() - timedelta(days=days)
    rows = (
        AdMetricDaily.objects.filter(entity_id__in=entity_ids, day__gte=since)
        .values("day")
        .annotate(
            i=Sum("impressions"), c=Sum("clicks"), s=Sum("spend_micros"), v=Sum("conversions")
        )
        .order_by("day")
    )
    return [
        {
            "day": r["day"].isoformat(),
            "impressions": r["i"],
            "clicks": r["c"],
            "spend": round(r["s"] / MICROS, 2),
            "conversions": round(float(r["v"] or 0), 2),
        }
        for r in rows
    ]
