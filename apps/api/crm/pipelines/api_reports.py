"""Pipeline insights and the CSV export of deals."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from django.http import HttpRequest, StreamingHttpResponse
from ninja import Query, Router, Schema

from audit.actors import Actor
from crm.common import csvwrite
from crm.pipelines import exports, services
from crm.pipelines.api import DealFilters
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace, resolve_workspace_for_file
from workspaces.permissions import Permission

router = Router(tags=["crm-pipelines"], auth=api_auth)


class InsightTotals(Schema):
    won: int
    won_value_micros: int
    lost: int
    win_rate_percent: float | None
    avg_days_to_win: float | None


class ForecastMonth(Schema):
    month: date | None
    count: int
    value_micros: int
    weighted_micros: int


class FunnelStage(Schema):
    stage_id: uuid.UUID
    name: str
    reached: int
    conversion_percent: float | None


class StageTime(Schema):
    stage_id: uuid.UUID
    name: str
    average_days: float | None
    deals: int


class LostReasonRow(Schema):
    reason_id: uuid.UUID | None
    name: str | None
    count: int
    value_micros: int


class OwnerRow(Schema):
    user_id: uuid.UUID | None
    open: int
    open_value_micros: int
    won: int
    won_value_micros: int
    lost: int


class PipelineInsightsOut(Schema):
    since: datetime
    until: datetime
    totals: InsightTotals
    forecast: list[ForecastMonth]
    funnel: list[FunnelStage]
    time_in_stage: list[StageTime]
    lost_reasons: list[LostReasonRow]
    owners: list[OwnerRow]


@router.get(
    "/pipelines/{pipeline_id}/insights",
    response=PipelineInsightsOut,
    operation_id="crm_pipelines_insights",
)
def pipeline_insights(
    request: HttpRequest,
    pipeline_id: uuid.UUID,
    since: datetime | None = None,
    until: datetime | None = None,
) -> PipelineInsightsOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW_REPORTS)
    found = services.pipeline_insights(
        membership.workspace_id, pipeline_id, membership, since=since, until=until
    )
    return PipelineInsightsOut(**found)


@router.get("/opportunities-export.csv", operation_id="crm_opportunities_export")
def export_opportunities(
    request: HttpRequest, filters: Query[DealFilters], sort: str | None = None
) -> StreamingHttpResponse:
    """The deals matching the same filters as the list, as a download."""
    membership = resolve_workspace_for_file(request, Permission.CRM_IMPORT_EXPORT)
    rows = exports.rows(
        membership=membership,
        actor=Actor.human(membership.user_id),
        sort=sort,
        **filters.model_dump(),
    )
    response = StreamingHttpResponse(csvwrite.write(rows), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="deals.csv"'
    return response
