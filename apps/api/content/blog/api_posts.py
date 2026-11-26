"""All blog posts of a site with their SEO score, and one-click AI improvement."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from content import checks, review, sync
from content.api_pages import PgApplied, _editable, propose_changes
from content.models import (
    AnalysisStatus,
    ContentItem,
    PageAnalysis,
    PageSuggestion,
    SuggestionStatus,
    SyncRun,
    SyncStatus,
    Website,
)
from content.tasks import analyze_page, sync_website
from core.errors import NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["blog"], auth=api_auth)

WEAK_BELOW = 70
BULK_LIMIT = 10
AI_FIELDS = ("seo_title", "meta_description", "title")
RUNNING = {AnalysisStatus.QUEUED, AnalysisStatus.RUNNING}
TOP_PROBLEMS = 3


class PostRow(Schema):
    id: uuid.UUID
    title: str
    url: str
    published_at: datetime | None
    modified_at: datetime | None
    word_count: int
    score: int
    problems: list[str]
    analysis_status: str
    analysis_verdict: str
    ai_fixes: list[str]
    advice_count: int


class PostList(Schema):
    posts: list[PostRow]
    average: int
    weak: int
    weak_below: int


class LibraryOut(Schema):
    """The copy of the site's posts the platform works from, and how fresh it is."""

    posts: int
    synced_at: datetime | None
    syncing: bool
    error: str


class BulkIn(Schema):
    below: int = WEAK_BELOW
    limit: int = BULK_LIMIT


class BulkOut(Schema):
    started: int


def _site(request: HttpRequest, website_id: uuid.UUID, permission: Permission) -> Website:
    membership = resolve_workspace(request, permission)
    site = Website.objects.filter(pk=website_id, workspace_id=membership.workspace_id).first()
    if site is None:
        raise NotFoundError("website not found")
    return site


def _post(site: Website, item_id: uuid.UUID) -> ContentItem:
    item = (
        ContentItem.objects.select_related("website__connection")
        .filter(pk=item_id, website=site, kind="post", removed_at__isnull=True)
        .first()
    )
    if item is None:
        raise NotFoundError("post not found")
    return item


def _latest_analyses(item_ids: list[uuid.UUID]) -> dict[uuid.UUID, PageAnalysis]:
    latest: dict[uuid.UUID, PageAnalysis] = {}
    for row in PageAnalysis.objects.filter(item_id__in=item_ids).order_by("-created_at"):
        latest.setdefault(row.item_id, row)
    return latest


def _open_suggestions(item_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[PageSuggestion]]:
    found: dict[uuid.UUID, list[PageSuggestion]] = {}
    rows = PageSuggestion.objects.filter(item_id__in=item_ids, status=SuggestionStatus.PROPOSED)
    for row in rows:
        found.setdefault(row.item_id, []).append(row)
    return found


def _row(
    item: ContentItem, analysis: PageAnalysis | None, suggestions: list[PageSuggestion]
) -> dict[str, Any]:
    found = checks.run_checks(item)
    weak = [c for c in found if c.status in {"fail", "warn"}]
    weak.sort(key=lambda c: (c.status != "fail", -c.weight))
    return {
        "id": item.pk,
        "title": item.title,
        "url": item.url,
        "published_at": item.published_at,
        "modified_at": item.modified_at,
        "word_count": int((item.stats or {}).get("word_count", 0)),
        "score": checks.score_of(found),
        "problems": [c.key for c in weak[:TOP_PROBLEMS]],
        "analysis_status": analysis.status if analysis else "",
        "analysis_verdict": str((analysis.result or {}).get("verdict", "")) if analysis else "",
        "ai_fixes": sorted({s.field for s in suggestions if s.field in AI_FIELDS}),
        "advice_count": sum(1 for s in suggestions if s.field == "advice"),
    }


def _posts(site: Website) -> list[ContentItem]:
    return list(
        ContentItem.objects.filter(
            website=site, kind="post", status="publish", removed_at__isnull=True
        )
    )


@router.get("/{website_id}/blog/posts", response=PostList, operation_id="blog_posts")
def list_posts(request: HttpRequest, website_id: uuid.UUID) -> dict[str, Any]:
    site = _site(request, website_id, Permission.VIEW)
    items = _posts(site)
    ids = [i.pk for i in items]
    analyses, suggestions = _latest_analyses(ids), _open_suggestions(ids)
    rows = sorted(
        (_row(i, analyses.get(i.pk), suggestions.get(i.pk, [])) for i in items),
        key=lambda r: (r["score"], r["title"]),
    )
    average = round(sum(r["score"] for r in rows) / len(rows)) if rows else 0
    weak = sum(1 for r in rows if r["score"] < WEAK_BELOW)
    return {"posts": rows, "average": average, "weak": weak, "weak_below": WEAK_BELOW}


def _library(site: Website) -> dict[str, Any]:
    runs = SyncRun.objects.filter(website=site, kind="inventory")
    last_done = runs.filter(status=SyncStatus.DONE).first()
    active = runs.filter(status__in=[SyncStatus.QUEUED, SyncStatus.RUNNING]).first()
    latest = runs.first()
    failed = latest is not None and latest.status == SyncStatus.FAILED
    return {
        "posts": len(_posts(site)),
        "synced_at": last_done.finished_at if last_done else None,
        "syncing": active is not None and not active.is_stale,
        "error": latest.error if failed and latest else "",
    }


@router.get("/{website_id}/blog/library", response=LibraryOut, operation_id="blog_library")
def library(request: HttpRequest, website_id: uuid.UUID) -> dict[str, Any]:
    return _library(_site(request, website_id, Permission.VIEW))


@router.post(
    "/{website_id}/blog/library/reload", response=LibraryOut, operation_id="blog_library_reload"
)
def reload_library(request: HttpRequest, website_id: uuid.UUID) -> dict[str, Any]:
    """Read the site again (only what changed since the last read). One at a time."""
    site = _site(request, website_id, Permission.EDIT_CONTENT)
    if not _library(site)["syncing"]:
        run = sync.start_run(site)
        sync_website.delay(str(run.pk))
    return _library(site)


def _start(item: ContentItem, user: User) -> bool:
    if item.analyses.filter(status__in=RUNNING).exists():
        return False
    analysis = review.start_analysis(item, requested_by=f"user:{user.pk}")
    analyze_page.delay(str(analysis.pk))
    return True


@router.post(
    "/{website_id}/blog/posts/{item_id}/improve", response=BulkOut, operation_id="blog_post_improve"
)
def improve(request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID) -> dict[str, int]:
    site = _site(request, website_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    return {"started": int(_start(_post(site, item_id), user))}


@router.post(
    "/{website_id}/blog/posts/improve-weak",
    response=BulkOut,
    operation_id="blog_posts_improve_weak",
)
def improve_weak(request: HttpRequest, website_id: uuid.UUID, payload: BulkIn) -> dict[str, int]:
    site = _site(request, website_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    weak = sorted(
        (i for i in _posts(site) if checks.score_of(checks.run_checks(i)) < payload.below),
        key=lambda i: checks.score_of(checks.run_checks(i)),
    )
    limit = max(1, min(payload.limit, BULK_LIMIT))
    return {"started": sum(_start(item, user) for item in weak[:limit])}


@router.post(
    "/{website_id}/blog/posts/{item_id}/apply-ai",
    response=PgApplied,
    operation_id="blog_post_apply_ai",
)
def apply_ai(request: HttpRequest, website_id: uuid.UUID, item_id: uuid.UUID) -> PgApplied:
    """Apply every open AI suggestion for the title, SEO title and meta description."""
    site = _site(request, website_id, Permission.EDIT_CONTENT)
    item = _post(site, item_id)
    user: User = request.auth  # type: ignore[attr-defined]
    allowed = set(_editable(item).fields) & set(AI_FIELDS)
    picked = [
        s
        for s in PageSuggestion.objects.filter(item=item, status=SuggestionStatus.PROPOSED)
        if s.field in allowed
    ]
    if not picked:
        raise ValidationFailedError("there are no AI fixes to apply to this post")
    changes = {s.field: s.proposed for s in picked}
    return propose_changes(item, changes, [s.field for s in picked], user)
