"""Read-only analysis of one pipeline: forecast, funnel, time in stage, losses and owners.

Every function takes the deals the viewer may see, so record scope is honoured. Sums are in the
stored currency; a pipeline is expected to use one.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from django.db.models import Avg, Count, F, QuerySet, Sum
from django.db.models.functions import TruncMonth

from crm.pipelines import stats
from crm.pipelines.models import Opportunity, OpportunityStatus, Pipeline, StageChange

SECONDS_PER_DAY = 86_400
DEAL_SAMPLE_LIMIT = 5_000
PERCENT = 100
OPEN = OpportunityStatus.OPEN
WON = OpportunityStatus.WON
LOST = OpportunityStatus.LOST
Deals = QuerySet[Opportunity]


def forecast_by_month(deals: Deals) -> list[dict[str, Any]]:
    """Open deals by the month they are expected to close; ``month`` is None without a date."""
    rows = (
        deals.filter(status=OPEN)
        .annotate(month=TruncMonth("expected_close_date"))
        .values("month")
        .annotate(
            count=Count("id"),
            value=Sum("value_micros"),
            weighted=Sum(stats.weighted_value(F("stage__probability"))),
        )
        .order_by("month")
    )
    return [
        {
            "month": r["month"],
            "count": int(r["count"]),
            "value_micros": int(r["value"] or 0),
            "weighted_micros": int(r["weighted"] or 0) // PERCENT,
        }
        for r in rows
    ]


def funnel(workspace_id: uuid.UUID, pipeline: Pipeline, deals: Deals) -> list[dict[str, Any]]:
    """Per stage: deals that ever reached it, and the share of the previous stage that did."""
    reached = dict(
        StageChange.objects.for_workspace(workspace_id)
        .filter(opportunity__in=deals)
        .values_list("to_stage_id")
        .annotate(n=Count("opportunity_id", distinct=True))
    )
    out: list[dict[str, Any]] = []
    previous: int | None = None
    for stage in pipeline.stages.all():
        count = int(reached.get(stage.pk, 0))
        rate = round(PERCENT * count / previous, 1) if previous else None
        out.append(
            {"stage_id": stage.pk, "name": stage.name, "reached": count, "conversion_percent": rate}
        )
        previous = count
    return out


def time_in_stage(
    workspace_id: uuid.UUID, pipeline: Pipeline, deals: Deals, now: datetime
) -> list[dict[str, Any]]:
    """Average days a deal spent in each stage, from its history; an open deal counts to now."""
    sample = list(deals.values_list("id", "status", "closed_at")[:DEAL_SAMPLE_LIMIT])
    ends = {pk: (now if status == OPEN else closed or now) for pk, status, closed in sample}
    history: dict[uuid.UUID, list[tuple[uuid.UUID | None, datetime]]] = defaultdict(list)
    changes = (
        StageChange.objects.for_workspace(workspace_id)
        .filter(opportunity_id__in=list(ends))
        .order_by("opportunity_id", "created_at", "id")
        .values_list("opportunity_id", "to_stage_id", "created_at")
    )
    for deal_id, entered_stage, at in changes:
        history[deal_id].append((entered_stage, at))
    spent: dict[uuid.UUID, list[float]] = defaultdict(list)
    for deal_id, steps in history.items():
        for index, (stage_id, entered) in enumerate(steps):
            left = steps[index + 1][1] if index + 1 < len(steps) else ends[deal_id]
            if stage_id is not None:
                spent[stage_id].append(max(0.0, (left - entered).total_seconds()))
    return [
        {
            "stage_id": stage.pk,
            "name": stage.name,
            "average_days": (
                round(sum(spent[stage.pk]) / len(spent[stage.pk]) / SECONDS_PER_DAY, 1)
                if spent.get(stage.pk)
                else None
            ),
            "deals": len(spent.get(stage.pk, [])),
        }
        for stage in pipeline.stages.all()
    ]


def lost_reasons(deals: Deals, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Deals lost in the period, grouped by reason (a null name means no reason was given)."""
    rows = (
        deals.filter(status=LOST, closed_at__gte=since, closed_at__lt=until)
        .values("lost_reason_id", "lost_reason__name")
        .annotate(count=Count("id"), value=Sum("value_micros"))
        .order_by("-count")
    )
    return [
        {
            "reason_id": r["lost_reason_id"],
            "name": r["lost_reason__name"],
            "count": int(r["count"]),
            "value_micros": int(r["value"] or 0),
        }
        for r in rows
    ]


def owners(deals: Deals, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Per owner: open pipeline, and deals won and lost in the period."""
    table: dict[Any, dict[str, int]] = defaultdict(
        lambda: {"open": 0, "open_value_micros": 0, "won": 0, "won_value_micros": 0, "lost": 0}
    )
    open_rows = (
        deals.filter(status=OPEN)
        .values("assigned_to_id")
        .annotate(n=Count("id"), v=Sum("value_micros"))
    )
    for row in open_rows:
        table[row["assigned_to_id"]].update(
            open=int(row["n"]), open_value_micros=int(row["v"] or 0)
        )
    closed = deals.filter(closed_at__gte=since, closed_at__lt=until)
    won_rows = (
        closed.filter(status=WON)
        .values("assigned_to_id")
        .annotate(n=Count("id"), v=Sum("value_micros"))
    )
    for won_row in won_rows:
        table[won_row["assigned_to_id"]].update(
            won=int(won_row["n"]), won_value_micros=int(won_row["v"] or 0)
        )
    lost_rows = closed.filter(status=LOST).values("assigned_to_id").annotate(n=Count("id"))
    for lost_row in lost_rows:
        table[lost_row["assigned_to_id"]]["lost"] = int(lost_row["n"])
    ranked = sorted(table.items(), key=lambda item: -item[1]["won_value_micros"])
    return [{"user_id": user_id, **numbers} for user_id, numbers in ranked]


def totals(deals: Deals, since: datetime, until: datetime) -> dict[str, Any]:
    """Won and lost in the period, win rate and the average days from creation to win."""
    closed = deals.filter(closed_at__gte=since, closed_at__lt=until)
    won = closed.filter(status=WON).aggregate(
        n=Count("id"), v=Sum("value_micros"), d=Avg(F("closed_at") - F("created_at"))
    )
    lost = closed.filter(status=LOST).count()
    won_count = int(won["n"] or 0)
    decided = won_count + lost
    return {
        "won": won_count,
        "won_value_micros": int(won["v"] or 0),
        "lost": lost,
        "win_rate_percent": round(PERCENT * won_count / decided, 1) if decided else None,
        "avg_days_to_win": (
            round(won["d"].total_seconds() / SECONDS_PER_DAY, 1) if won["d"] else None
        ),
    }
