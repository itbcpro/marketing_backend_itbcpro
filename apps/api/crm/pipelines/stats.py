"""Read-only aggregates over deals, for reports."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db.models import Avg, BigIntegerField, Count, ExpressionWrapper, F, QuerySet, Sum
from django.db.models.expressions import Combinable
from django.db.models.functions import Coalesce

from crm.pipelines.models import Opportunity, OpportunityStatus, Pipeline, StageChange

SECONDS_PER_DAY = 86_400
PERCENT = 100


def weighted_value(stage_probability: Combinable) -> Combinable:
    """Value times win percent for each deal: its own probability, else the stage's.
    Sum it and divide by 100 to get micros."""
    probability = Coalesce(
        F("probability_override"), stage_probability, output_field=BigIntegerField()
    )
    return ExpressionWrapper(F("value_micros") * probability, output_field=BigIntegerField())


def summarize(rows: QuerySet[Opportunity]) -> dict[str, int]:
    """Count, value and probability-weighted value of ``rows``."""
    row = rows.aggregate(
        count=Count("id"),
        value=Sum("value_micros"),
        weighted=Sum(weighted_value(F("stage__probability"))),
    )
    return {
        "count": int(row["count"] or 0),
        "value_micros": int(row["value"] or 0),
        "weighted_micros": int(row["weighted"] or 0) // PERCENT,
    }


def outcomes(rows: QuerySet[Opportunity]) -> dict[str, int]:
    """Won and lost deal counts of ``rows``, for the win rate."""
    counts = dict(
        rows.filter(status__in=[OpportunityStatus.WON, OpportunityStatus.LOST])
        .values_list("status")
        .annotate(n=Count("id"))
    )
    return {"won": int(counts.get("won", 0)), "lost": int(counts.get("lost", 0))}


def pipeline_report(
    workspace_id: uuid.UUID, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Per pipeline: open value by stage, deals won and lost in the period, and stage entries."""
    out: list[dict[str, Any]] = []
    deals = Opportunity.objects.for_workspace(workspace_id)
    for pipeline in Pipeline.objects.for_workspace(workspace_id).filter(archived_at__isnull=True):
        entered = dict(
            StageChange.objects.for_workspace(workspace_id)
            .filter(opportunity__pipeline=pipeline, created_at__gte=since, created_at__lt=until)
            .values_list("to_stage_id")
            .annotate(n=Count("id"))
        )
        stages = []
        for stage in pipeline.stages.all():
            open_rows = deals.filter(stage=stage, status=OpportunityStatus.OPEN).aggregate(
                count=Count("id"), value=Sum("value_micros")
            )
            stages.append(
                {
                    "stage_id": str(stage.pk),
                    "name": stage.name,
                    "open_count": open_rows["count"] or 0,
                    "open_value_micros": int(open_rows["value"] or 0),
                    "entered": int(entered.get(stage.pk, 0)),
                }
            )
        closed = deals.filter(pipeline=pipeline, closed_at__gte=since, closed_at__lt=until)
        won = closed.filter(status=OpportunityStatus.WON).aggregate(
            count=Count("id"),
            value=Sum("value_micros"),
            duration=Avg(F("closed_at") - F("created_at")),
        )
        out.append(
            {
                "pipeline_id": str(pipeline.pk),
                "name": pipeline.name,
                "stages": stages,
                "won": won["count"] or 0,
                "won_value_micros": int(won["value"] or 0),
                "lost": closed.filter(status=OpportunityStatus.LOST).count(),
                "avg_days_to_win": (
                    round(won["duration"].total_seconds() / SECONDS_PER_DAY, 1)
                    if won["duration"]
                    else None
                ),
            }
        )
    return out


def won_value_by_contact(
    workspace_id: uuid.UUID, since: datetime, until: datetime
) -> dict[uuid.UUID, int]:
    """Value of deals won in the period, summed per contact."""
    rows = (
        Opportunity.objects.for_workspace(workspace_id)
        .filter(status=OpportunityStatus.WON, closed_at__gte=since, closed_at__lt=until)
        .values("contact_id")
        .annotate(value=Sum("value_micros"))
    )
    return {r["contact_id"]: int(r["value"] or 0) for r in rows}


def won_by_owner(
    workspace_id: uuid.UUID, since: datetime, until: datetime
) -> dict[str, dict[str, int]]:
    rows = (
        Opportunity.objects.for_workspace(workspace_id)
        .filter(
            status=OpportunityStatus.WON,
            assigned_to__isnull=False,
            closed_at__gte=since,
            closed_at__lt=until,
        )
        .values("assigned_to_id")
        .annotate(count=Count("id"), value=Sum("value_micros"))
    )
    return {
        str(r["assigned_to_id"]): {"count": r["count"], "value_micros": int(r["value"] or 0)}
        for r in rows
    }
