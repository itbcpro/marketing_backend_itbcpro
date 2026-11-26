"""Inventory screens: sync runs, the page list with filters, one page in detail."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Schema

from content import selectors, sync
from content.models import ContentItem, SyncRun, Website
from content.tasks import sync_website
from core.errors import NotFoundError
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["websites"], auth=api_auth)
MAX_PAGE_SIZE = 100
TEXT_PREVIEW = 2000


class InvSyncRunOut(Schema):
    id: uuid.UUID
    status: str
    stale: bool
    full: bool
    started_at: datetime | None
    finished_at: datetime | None
    stats: dict[str, Any]
    error: str


class InvSyncIn(Schema):
    full: bool = False


class InvItemOut(Schema):
    id: uuid.UUID
    kind: str
    title: str
    url: str
    status: str
    language: str
    author: str
    published_at: datetime | None
    modified_at: datetime | None
    word_count: int
    in_sitemap: bool
    http_status: int | None
    seo: dict[str, Any]
    issues: list[str]


class InvItemDetail(InvItemOut):
    terms: dict[str, Any]
    outline: list[dict[str, Any]]
    stats: dict[str, Any]
    text_preview: str


class InvItemPage(Schema):
    total: int
    page: int
    page_size: int
    items: list[InvItemOut]


class InvFilters(Schema):
    kind: str = ""
    status: str = ""
    issue: str = ""
    q: str = ""
    removed: bool = False
    sort: str = "modified"
    page: int = 1
    page_size: int = 25


def _site(request: HttpRequest, website_id: uuid.UUID, permission: Permission) -> Website:
    membership = resolve_workspace(request, permission)
    site = Website.objects.filter(pk=website_id, workspace_id=membership.workspace_id).first()
    if site is None:
        raise NotFoundError("website not found")
    return site


def _run_out(run: SyncRun) -> InvSyncRunOut:
    return InvSyncRunOut(
        id=run.pk,
        status=run.status,
        stale=run.is_stale,
        full=run.full,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stats=run.stats,
        error=run.error,
    )


def _item_out(item: ContentItem) -> InvItemOut:
    return InvItemOut(
        id=item.pk,
        kind=item.kind,
        title=item.title,
        url=item.url,
        status=item.status,
        language=item.language,
        author=item.author,
        published_at=item.published_at,
        modified_at=item.modified_at,
        word_count=int(item.stats.get("word_count", 0)),
        in_sitemap=item.in_sitemap,
        http_status=item.http_status,
        seo=item.seo,
        issues=selectors.issues_of(item),
    )


@router.post("/{website_id}/sync", response=InvSyncRunOut, operation_id="websites_sync")
def start_sync(request: HttpRequest, website_id: uuid.UUID, payload: InvSyncIn) -> InvSyncRunOut:
    site = _site(request, website_id, Permission.MANAGE_CONNECTIONS)
    run = sync.start_run(site, full=payload.full)
    sync_website.delay(str(run.pk))
    run.refresh_from_db()
    return _run_out(run)


@router.get("/{website_id}/sync-runs", response=list[InvSyncRunOut], operation_id="websites_runs")
def list_runs(request: HttpRequest, website_id: uuid.UUID) -> list[InvSyncRunOut]:
    site = _site(request, website_id, Permission.VIEW)
    return [_run_out(r) for r in SyncRun.objects.filter(website=site, kind="inventory")[:10]]


@router.get(
    "/{website_id}/inventory/summary", response=dict[str, Any], operation_id="websites_summary"
)
def inventory_summary(request: HttpRequest, website_id: uuid.UUID) -> dict[str, Any]:
    return selectors.summary(_site(request, website_id, Permission.VIEW))


@router.get("/{website_id}/inventory", response=InvItemPage, operation_id="websites_inventory")
def inventory(
    request: HttpRequest, website_id: uuid.UUID, filters: Query[InvFilters]
) -> InvItemPage:
    site = _site(request, website_id, Permission.VIEW)
    rows = selectors.filtered(
        site,
        kind=filters.kind,
        status=filters.status,
        issue=filters.issue,
        q=filters.q,
        removed=filters.removed,
        sort=filters.sort,
    )
    size = max(1, min(filters.page_size, MAX_PAGE_SIZE))
    page = max(1, filters.page)
    return InvItemPage(
        total=rows.count(),
        page=page,
        page_size=size,
        items=[_item_out(i) for i in rows[(page - 1) * size : page * size]],
    )


@router.get(
    "/{website_id}/inventory/{item_id}", response=InvItemDetail, operation_id="websites_item"
)
def item_detail(request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID) -> InvItemDetail:
    site = _site(request, website_id, Permission.VIEW)
    item = ContentItem.objects.filter(website=site, pk=item_id).first()
    if item is None:
        raise NotFoundError("page not found")
    return InvItemDetail(
        **_item_out(item).dict(),
        terms=item.terms,
        outline=item.outline,
        stats=item.stats,
        text_preview=item.text[:TEXT_PREVIEW],
    )
