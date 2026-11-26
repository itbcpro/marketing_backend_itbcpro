"""List rows for campaigns, ad groups and ads with numbers rolled up from their ads."""

import uuid
from datetime import timedelta
from typing import Any

from django.db.models import Sum
from django.utils import timezone

from ads.models import AdEntity, AdLevel, AdMetricDaily, AdReview

MICROS = 1_000_000
LEAF_LEVELS = frozenset({AdLevel.AD, AdLevel.ASSET_GROUP})


def _window(ids: list[uuid.UUID], days: int) -> dict[uuid.UUID, dict[str, Any]]:
    since = timezone.now().date() - timedelta(days=days)
    rows = (
        AdMetricDaily.objects.filter(entity_id__in=ids, day__gte=since)
        .values("entity_id")
        .annotate(
            i=Sum("impressions"),
            c=Sum("clicks"),
            s=Sum("spend_micros"),
            v=Sum("conversions"),
            w=Sum("conversion_value"),
        )
    )
    return {
        r["entity_id"]: {"i": r["i"], "c": r["c"], "s": r["s"], "v": r["v"], "w": r["w"]}
        for r in rows
    }


def rollup(entities: list[AdEntity], days: int) -> dict[uuid.UUID, dict[str, float]]:
    """Totals for every entity, summed over the ads or asset groups beneath it.

    Keywords keep their own numbers: they overlap the ads above them, so they are never summed up.
    """
    by_id = {e.pk: e for e in entities}
    measured = [e for e in entities if e.level in LEAF_LEVELS or e.level == AdLevel.KEYWORD]
    per_entity = _window([e.pk for e in measured], days)
    totals: dict[uuid.UUID, dict[str, float]] = {}
    for leaf in measured:
        numbers = per_entity.get(leaf.pk)
        if not numbers:
            continue
        if leaf.level == AdLevel.KEYWORD:
            totals[leaf.pk] = {k: float(v or 0) for k, v in numbers.items()}
            continue
        node: AdEntity | None = leaf
        while node is not None:
            acc = totals.setdefault(node.pk, {"i": 0, "c": 0, "s": 0, "v": 0.0, "w": 0.0})
            for key in ("i", "c", "s", "v", "w"):
                acc[key] += float(numbers.get(key) or 0)
            parent_id = node.parent_id
            node = by_id.get(parent_id) if parent_id is not None else None
    return totals


def _grandparent_id(entity: AdEntity, by_id: dict[uuid.UUID, AdEntity]) -> uuid.UUID | None:
    parent = by_id.get(entity.parent_id) if entity.parent_id is not None else None
    return parent.parent_id if parent else None


def _issue_count(entity: AdEntity, by_id: dict[uuid.UUID, AdEntity]) -> int:
    count = 0
    node: AdEntity | None = entity
    while node is not None:
        count += len(node.data.get("serving_issues") or [])
        node = by_id.get(node.parent_id) if node.parent_id else None
    return count


def _bid_too_low(
    entity: AdEntity, children: dict[uuid.UUID, list[AdEntity]], by_id: dict[uuid.UUID, AdEntity]
) -> bool:
    if entity.level == AdLevel.AD:
        parent = by_id.get(entity.parent_id) if entity.parent_id is not None else None
        return bool(parent.data.get("bid_too_low")) if parent else False
    if entity.level == AdLevel.AD_GROUP:
        return bool(entity.data.get("bid_too_low"))
    if entity.level == AdLevel.CAMPAIGN:
        return any(bool(g.data.get("bid_too_low")) for g in children.get(entity.pk, []))
    return False


def latest_verdicts(ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    return {
        r.entity_id: (r.result or {})
        for r in AdReview.objects.filter(entity_id__in=ids, status="done")
        .order_by("entity_id", "-created_at")
        .distinct("entity_id")
        if r.entity_id is not None
    }


def build_rows(entities: list[AdEntity]) -> dict[uuid.UUID, dict[str, Any]]:
    """One row per entity. ``entities`` must include every ancestor of each entity in it."""
    by_id = {e.pk: e for e in entities}
    children: dict[uuid.UUID, list[AdEntity]] = {}
    for e in entities:
        if e.parent_id:
            children.setdefault(e.parent_id, []).append(e)
    week, month = rollup(entities, 7), rollup(entities, 30)
    verdicts = latest_verdicts([e.pk for e in entities])
    rows: dict[uuid.UUID, dict[str, Any]] = {}
    for e in entities:
        w, m = week.get(e.pk, {}), month.get(e.pk, {})
        impressions, clicks = int(w.get("i") or 0), int(w.get("c") or 0)
        group = (
            e if e.level == AdLevel.AD_GROUP else by_id.get(e.parent_id) if e.parent_id else None
        )
        campaign = (
            e
            if e.level == AdLevel.CAMPAIGN
            else (by_id.get(group.parent_id) if group and group.parent_id else None)
        )
        creative = e.data.get("creative") or {}
        result = verdicts.get(e.pk, {})
        ads_below = [
            a
            for a in entities
            if a.level in LEAF_LEVELS and e.pk in {a.pk, a.parent_id, _grandparent_id(a, by_id)}
        ]
        rows[e.pk] = {
            "entity_id": e.pk,
            "level": e.level,
            "name": e.name,
            "title": str(creative.get("title") or e.name),
            "body": str(creative.get("body") or e.data.get("description") or ""),
            "status": e.status,
            "review_status": e.review_status,
            "campaign": campaign.name if campaign else "",
            "ad_group": group.name if group and e.level != AdLevel.CAMPAIGN else "",
            "impressions_7d": impressions,
            "clicks_7d": clicks,
            "spend_7d": round(int(w.get("s") or 0) / MICROS, 2),
            "ctr_7d": round(clicks / impressions, 4) if impressions else 0.0,
            "clicks_30d": int(m.get("c") or 0),
            "spend_30d": round(int(m.get("s") or 0) / MICROS, 2),
            "conversions_7d": round(float(w.get("v") or 0), 2),
            "conversions_30d": round(float(m.get("v") or 0), 2),
            "issue_count": _issue_count(e, by_id),
            "bid_too_low": _bid_too_low(e, children, by_id),
            "verdict": result.get("verdict"),
            "score": result.get("score"),
            "ads_count": len(ads_below),
            "children_count": len(children.get(e.pk, [])),
        }
    return rows
