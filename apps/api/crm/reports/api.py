"""Reports. Aggregates only; they need the reports permission and never expose message text."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema

from crm.reports import queries
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-reports"], auth=api_auth)


class ReportSpeed(Schema):
    count: int
    median_seconds: int | None
    p90_seconds: int | None
    average_seconds: int | None


class ReportSpeedByResponder(ReportSpeed):
    responder_type: str


class ReportSpeedByChannel(ReportSpeed):
    channel: str


class ReportResponseTime(ReportSpeed):
    waiting_now: int
    by_responder: list[ReportSpeedByResponder]
    by_channel: list[ReportSpeedByChannel]


class ReportDeliveryError(Schema):
    code: str
    count: int


class ReportDelivery(Schema):
    channel: str
    statuses: dict[str, int]
    delivery_rate: float | None
    errors: list[ReportDeliveryError]


class ReportStage(Schema):
    stage_id: uuid.UUID
    name: str
    open_count: int
    open_value_micros: int
    entered: int


class ReportPipeline(Schema):
    pipeline_id: uuid.UUID
    name: str
    stages: list[ReportStage]
    won: int
    won_value_micros: int
    lost: int
    avg_days_to_win: float | None


class ReportSource(Schema):
    source: str
    contacts: int
    won_value_micros: int


class ReportCampaign(Schema):
    campaign_id: str
    contacts: int


class ReportAttribution(Schema):
    sources: list[ReportSource]
    campaigns: list[ReportCampaign]


class ReportAgent(Schema):
    user_id: str
    name: str
    messages_sent: int
    tasks_completed: int
    deals_won: int
    won_value_micros: int
    median_first_response_seconds: int | None


def _scope(
    request: HttpRequest, since: datetime | None, until: datetime | None
) -> tuple[uuid.UUID, datetime, datetime]:
    membership = resolve_workspace(request, Permission.CRM_VIEW_REPORTS)
    start, end = queries.period(since, until)
    return membership.workspace_id, start, end


@router.get(
    "/reports/response-time", response=ReportResponseTime, operation_id="crm_report_response_time"
)
def response_time(
    request: HttpRequest, since: datetime | None = None, until: datetime | None = None
) -> dict[str, object]:
    workspace_id, start, end = _scope(request, since, until)
    return queries.response_time(workspace_id, start, end)


@router.get("/reports/delivery", response=list[ReportDelivery], operation_id="crm_report_delivery")
def delivery(
    request: HttpRequest, since: datetime | None = None, until: datetime | None = None
) -> list[dict[str, object]]:
    workspace_id, start, end = _scope(request, since, until)
    return queries.delivery(workspace_id, start, end)


@router.get("/reports/pipeline", response=list[ReportPipeline], operation_id="crm_report_pipeline")
def pipeline(
    request: HttpRequest, since: datetime | None = None, until: datetime | None = None
) -> list[dict[str, object]]:
    workspace_id, start, end = _scope(request, since, until)
    return queries.pipeline(workspace_id, start, end)


@router.get(
    "/reports/attribution", response=ReportAttribution, operation_id="crm_report_attribution"
)
def attribution(
    request: HttpRequest, since: datetime | None = None, until: datetime | None = None
) -> dict[str, object]:
    workspace_id, start, end = _scope(request, since, until)
    return queries.attribution(workspace_id, start, end)


@router.get("/reports/agents", response=list[ReportAgent], operation_id="crm_report_agents")
def agents(
    request: HttpRequest, since: datetime | None = None, until: datetime | None = None
) -> list[dict[str, object]]:
    workspace_id, start, end = _scope(request, since, until)
    return queries.agents(workspace_id, start, end)
