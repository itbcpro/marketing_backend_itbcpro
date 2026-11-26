"""Search screens: connect Google, link a property, sync, and read search performance."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Schema, Status

from connections.api import _redirect_uri as connection_redirect_uri
from connections.models import Connection, ConnectionStatus
from content import gsc_sync, search
from content.google.credentials import app_credentials
from content.google.gsc import PROVIDER
from content.models import ContentItem, GscSitemap, SearchProperty, SyncRun, Website
from content.tasks import sync_search
from core.errors import DomainError, NotFoundError
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["websites"], auth=api_auth)
DEFAULT_DAYS = 28


class SrConnection(Schema):
    id: uuid.UUID
    display_name: str
    status: str
    health_detail: str


class SrProperty(Schema):
    site_url: str
    permission: str
    connection_id: uuid.UUID | None
    first_date: str | None
    last_date: str | None
    last_synced_at: datetime | None
    last_error: str


class SrRun(Schema):
    id: uuid.UUID
    status: str
    stale: bool
    full: bool
    started_at: datetime | None
    finished_at: datetime | None
    stats: dict[str, Any]
    error: str


class SrStatus(Schema):
    keys_missing: list[str]
    redirect_uri: str
    connections: list[SrConnection]
    property: SrProperty | None
    last_run: SrRun | None


class SrSites(Schema):
    sites: list[dict[str, str]]
    suggested: str | None


class SrLinkIn(Schema):
    connection_id: uuid.UUID
    site_url: str


class SrSyncIn(Schema):
    full: bool = False


class SrFilters(Schema):
    days: int = DEFAULT_DAYS
    q: str = ""


class SrSitemap(Schema):
    path: str
    is_index: bool
    is_pending: bool
    last_submitted: datetime | None
    last_downloaded: datetime | None
    errors: int
    warnings: int
    contents: list[dict[str, Any]]


def _site(request: HttpRequest, website_id: uuid.UUID, permission: Permission) -> Website:
    membership = resolve_workspace(request, permission)
    site = Website.objects.filter(pk=website_id, workspace_id=membership.workspace_id).first()
    if site is None:
        raise NotFoundError("website not found")
    return site


def _run(run: SyncRun) -> SrRun:
    return SrRun(
        id=run.pk,
        status=run.status,
        stale=run.is_stale,
        full=run.full,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stats=run.stats,
        error=run.error,
    )


def _property(prop: SearchProperty) -> SrProperty:
    return SrProperty(
        site_url=prop.site_url,
        permission=prop.permission,
        connection_id=prop.connection_id,
        first_date=prop.first_date.isoformat() if prop.first_date else None,
        last_date=prop.last_date.isoformat() if prop.last_date else None,
        last_synced_at=prop.last_synced_at,
        last_error=prop.last_error,
    )


@router.get("/{website_id}/search", response=SrStatus, operation_id="website_search_status")
def status(request: HttpRequest, website_id: uuid.UUID) -> SrStatus:
    site = _site(request, website_id, Permission.VIEW)
    prop = SearchProperty.objects.filter(website=site).first()
    run = SyncRun.objects.filter(website=site, kind="search").first()
    connections = Connection.objects.filter(
        workspace_id=site.workspace_id, provider=PROVIDER
    ).exclude(status=ConnectionStatus.REVOKED)
    return SrStatus(
        keys_missing=app_credentials(site.workspace_id).missing(),
        redirect_uri=connection_redirect_uri(PROVIDER),
        connections=[
            SrConnection(
                id=c.pk, display_name=c.display_name, status=c.status, health_detail=c.health_detail
            )
            for c in connections
        ],
        property=_property(prop) if prop else None,
        last_run=_run(run) if run else None,
    )


@router.get(
    "/{website_id}/search/properties/{connection_id}",
    response=SrSites,
    operation_id="website_search_properties",
)
def properties(request: HttpRequest, website_id: uuid.UUID, connection_id: uuid.UUID) -> SrSites:
    """The Search Console properties this Google account can read, with the best match marked."""
    site = _site(request, website_id, Permission.MANAGE_CONNECTIONS)
    connection = search.google_connection(site.workspace_id, connection_id)
    found = search.candidates(site, connection)
    return SrSites(sites=found["sites"], suggested=found["suggested"])


@router.post("/{website_id}/search/link", response=SrStatus, operation_id="website_search_link")
def link(request: HttpRequest, website_id: uuid.UUID, payload: SrLinkIn) -> SrStatus:
    site = _site(request, website_id, Permission.MANAGE_CONNECTIONS)
    connection = search.google_connection(site.workspace_id, payload.connection_id)
    search.link(site, connection, payload.site_url)
    run = gsc_sync.start_run(site, full=True)
    sync_search.delay(str(run.pk))
    return status(request, website_id)


@router.delete(
    "/{website_id}/search/link", response={204: None}, operation_id="website_search_unlink"
)
def unlink(request: HttpRequest, website_id: uuid.UUID) -> Status[None]:
    search.unlink(_site(request, website_id, Permission.MANAGE_CONNECTIONS))
    return Status(204, None)


@router.post("/{website_id}/search/sync", response=SrRun, operation_id="website_search_sync")
def sync(request: HttpRequest, website_id: uuid.UUID, payload: SrSyncIn) -> SrRun:
    site = _site(request, website_id, Permission.MANAGE_CONNECTIONS)
    if not SearchProperty.objects.filter(website=site).exists():
        raise DomainError("no_search_property")
    run = gsc_sync.start_run(site, full=payload.full)
    sync_search.delay(str(run.pk))
    run.refresh_from_db()
    return _run(run)


@router.get(
    "/{website_id}/search/overview", response=dict[str, Any], operation_id="website_search_overview"
)
def overview(
    request: HttpRequest, website_id: uuid.UUID, filters: Query[SrFilters]
) -> dict[str, Any]:
    return search.overview(_site(request, website_id, Permission.VIEW), filters.days)


@router.get(
    "/{website_id}/search/queries",
    response=list[dict[str, Any]],
    operation_id="website_search_queries",
)
def queries(
    request: HttpRequest, website_id: uuid.UUID, filters: Query[SrFilters]
) -> list[dict[str, Any]]:
    site = _site(request, website_id, Permission.VIEW)
    prop = SearchProperty.objects.filter(website=site).first()
    window = search.period(filters.days, last=prop.last_date if prop else None)
    if window is None:
        return []
    return search.top_queries(site, window[0], window[1], text=filters.q)


@router.get(
    "/{website_id}/search/pages", response=list[dict[str, Any]], operation_id="website_search_pages"
)
def pages(
    request: HttpRequest, website_id: uuid.UUID, filters: Query[SrFilters]
) -> list[dict[str, Any]]:
    site = _site(request, website_id, Permission.VIEW)
    prop = SearchProperty.objects.filter(website=site).first()
    window = search.period(filters.days, last=prop.last_date if prop else None)
    if window is None:
        return []
    return search.top_pages(site, window[0], window[1])


@router.get(
    "/{website_id}/search/sitemaps",
    response=list[SrSitemap],
    operation_id="website_search_sitemaps",
)
def sitemaps(request: HttpRequest, website_id: uuid.UUID) -> list[SrSitemap]:
    site = _site(request, website_id, Permission.VIEW)
    return [
        SrSitemap(
            path=s.path,
            is_index=s.is_index,
            is_pending=s.is_pending,
            last_submitted=s.last_submitted,
            last_downloaded=s.last_downloaded,
            errors=s.errors,
            warnings=s.warnings,
            contents=s.contents,
        )
        for s in GscSitemap.objects.filter(website=site).order_by("path")
    ]


@router.get(
    "/{website_id}/pages/{item_id}/search",
    response=dict[str, Any],
    operation_id="website_page_search",
)
def page_search(
    request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID, filters: Query[SrFilters]
) -> dict[str, Any]:
    site = _site(request, website_id, Permission.VIEW)
    item = ContentItem.objects.filter(pk=item_id, website=site).first()
    if item is None:
        raise NotFoundError("page not found")
    return search.page_search(site, item.url, filters.days)
