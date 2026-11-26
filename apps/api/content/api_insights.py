"""Search reports with filters, comparison, AI analysis and Google PageSpeed Insights."""

import uuid
from datetime import date
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Schema

from content import search_ai, search_report, speed
from content.models import SearchAnalysis, Website
from core.errors import NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["websites"], auth=api_auth)
RECENT_ANALYSES = 10


class ReportFilters(Schema):
    days: int = 28
    start: date | None = None
    end: date | None = None
    compare: str = "previous"
    q: str = ""
    page: str = ""


class RowFilters(ReportFilters):
    kind: str = "query"
    sort: str = "clicks"
    min_impressions: int = 0
    status: str = ""
    limit: int = 100


class AnalysisIn(Schema):
    start: date | None = None
    end: date | None = None
    days: int = 28
    compare: str = "previous"
    instruction: str = ""


class SpeedIn(Schema):
    urls: list[str] = []
    strategies: list[str] = ["mobile", "desktop"]
    top: int = 0


class SpeedHistoryFilters(Schema):
    url: str
    strategy: str = "mobile"


def _site(request: HttpRequest, website_id: uuid.UUID, permission: Permission) -> Website:
    membership = resolve_workspace(request, permission)
    site = Website.objects.filter(pk=website_id, workspace_id=membership.workspace_id).first()
    if site is None:
        raise NotFoundError("website not found")
    return site


def _window(site: Website, f: ReportFilters | AnalysisIn) -> search_report.Window | None:
    if f.compare not in search_report.COMPARE_MODES:
        raise ValidationFailedError("unknown comparison")
    return search_report.resolve_window(
        site, days=f.days, start=f.start, end=f.end, compare=f.compare
    )


@router.get(
    "/{website_id}/search/report", response=dict[str, Any], operation_id="website_search_report"
)
def search_report_endpoint(
    request: HttpRequest, website_id: uuid.UUID, filters: Query[ReportFilters]
) -> dict[str, Any]:
    site = _site(request, website_id, Permission.VIEW)
    window = _window(site, filters)
    return search_report.report(site, window, text=filters.q.strip(), page=filters.page.strip())


@router.get(
    "/{website_id}/search/rows", response=list[dict[str, Any]], operation_id="website_search_rows"
)
def search_rows(
    request: HttpRequest, website_id: uuid.UUID, filters: Query[RowFilters]
) -> list[dict[str, Any]]:
    site = _site(request, website_id, Permission.VIEW)
    if filters.kind not in {"query", "page"}:
        raise ValidationFailedError("kind must be query or page")
    return search_report.rows(
        site,
        _window(site, filters),
        kind=filters.kind,
        text=filters.q.strip(),
        page=filters.page.strip(),
        sort=filters.sort,
        min_impressions=max(0, filters.min_impressions),
        status=filters.status if filters.status in search_report.STATUSES else "",
        limit=max(1, min(filters.limit, 500)),
    )


@router.get(
    "/{website_id}/search/opportunities",
    response=dict[str, Any],
    operation_id="website_search_opportunities",
)
def search_opportunities(
    request: HttpRequest, website_id: uuid.UUID, filters: Query[ReportFilters]
) -> dict[str, Any]:
    site = _site(request, website_id, Permission.VIEW)
    return search_report.opportunities(site, _window(site, filters))


@router.get(
    "/{website_id}/search/analyses",
    response=list[dict[str, Any]],
    operation_id="website_search_analyses",
)
def list_analyses(request: HttpRequest, website_id: uuid.UUID) -> list[dict[str, Any]]:
    site = _site(request, website_id, Permission.VIEW)
    rows = SearchAnalysis.objects.filter(website=site)[:RECENT_ANALYSES]
    return [search_ai.serialize(a) for a in rows]


@router.post(
    "/{website_id}/search/analyses",
    response=dict[str, Any],
    operation_id="website_search_analyze",
)
def analyze(request: HttpRequest, website_id: uuid.UUID, body: AnalysisIn) -> dict[str, Any]:
    site = _site(request, website_id, Permission.EDIT_CONTENT)
    window = _window(site, body)
    if window is None:
        raise ValidationFailedError("there is no search data to analyse yet")
    user: User = request.auth  # type: ignore[attr-defined]
    analysis = search_ai.start(
        site,
        window,
        compare=body.compare,
        instruction=body.instruction,
        requested_by=f"user:{user.pk}",
    )
    return search_ai.serialize(analysis)


@router.get(
    "/{website_id}/search/analyses/{analysis_id}",
    response=dict[str, Any],
    operation_id="website_search_analysis",
)
def get_analysis(
    request: HttpRequest, website_id: uuid.UUID, analysis_id: uuid.UUID
) -> dict[str, Any]:
    site = _site(request, website_id, Permission.VIEW)
    found = search_ai.get(site.pk, analysis_id)
    if found is None:
        raise NotFoundError("analysis not found")
    return search_ai.serialize(found)


@router.get(
    "/{website_id}/speed", response=list[dict[str, Any]], operation_id="website_speed_latest"
)
def speed_latest(request: HttpRequest, website_id: uuid.UUID) -> list[dict[str, Any]]:
    return speed.latest(_site(request, website_id, Permission.VIEW))


@router.get(
    "/{website_id}/speed/history",
    response=list[dict[str, Any]],
    operation_id="website_speed_history",
)
def speed_history(
    request: HttpRequest, website_id: uuid.UUID, filters: Query[SpeedHistoryFilters]
) -> list[dict[str, Any]]:
    site = _site(request, website_id, Permission.VIEW)
    return speed.history(site, filters.url, filters.strategy)


@router.post("/{website_id}/speed", response=list[dict[str, Any]], operation_id="website_speed_run")
def speed_run(request: HttpRequest, website_id: uuid.UUID, body: SpeedIn) -> list[dict[str, Any]]:
    site = _site(request, website_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    urls = body.urls or speed.top_urls(site, max(1, min(body.top or 5, speed.MAX_URLS)))
    started = speed.start(site, urls, body.strategies, requested_by=f"user:{user.pk}")
    return [speed.serialize(r) for r in started]
