"""Copy Search Console data (daily totals, pages, query and page pairs, sitemaps) into the database.

Google restates recent days, so every run re-reads the last few days. The first run backfills about
sixteen months, the longest history the API keeps.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from content.google.gsc import SearchConsoleClient
from content.models import (
    GscDaily,
    GscPageDaily,
    GscQueryDaily,
    GscSitemap,
    SearchProperty,
    SyncRun,
    SyncStatus,
    Website,
)
from core.errors import AuthExpiredError, DomainError
from core.locks import LockNotAcquiredError, redis_lock
from core.logging import get_logger

log = get_logger(__name__)

DATA_LAG_DAYS = 2
BACKFILL_DAYS = 480
OVERLAP_DAYS = 6
CHUNK_DAYS = 30
BATCH = 2000
QUERY_MAX, PAGE_MAX = 500, 1000
MIN_SITEMAP_PARTS = 2


def today() -> date:
    return datetime.now(UTC).date()


def sync_window(prop: SearchProperty, *, full: bool, now: date | None = None) -> tuple[date, date]:
    """First and last day to read: everything on a first or full run, else the recent overlap."""
    end = (now or today()) - timedelta(days=DATA_LAG_DAYS)
    if full or prop.last_date is None:
        return end - timedelta(days=BACKFILL_DAYS - 1), end
    return min(end, prop.last_date) - timedelta(days=OVERLAP_DAYS - 1), end


def chunks(start: date, end: date, size: int = CHUNK_DAYS) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=size - 1), end)
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out


def _day(value: str) -> date:
    return date.fromisoformat(value)


def _store[T](
    model: type[T],
    website: Website,
    rows: list[dict[str, Any]],
    build: Callable[[list[str]], dict[str, Any]],
    unique: list[str],
) -> int:
    objects = [
        model(  # type: ignore[call-arg]
            workspace_id=website.workspace_id,
            website=website,
            clicks=int(row.get("clicks", 0)),
            impressions=int(row.get("impressions", 0)),
            position=float(row.get("position", 0.0)),
            **build(list(row["keys"])),
        )
        for row in rows
        if row.get("keys")
    ]
    for start in range(0, len(objects), BATCH):
        model.objects.bulk_create(  # type: ignore[attr-defined]
            objects[start : start + BATCH],
            update_conflicts=True,
            unique_fields=unique,
            update_fields=["clicks", "impressions", "position"],
        )
    return len(objects)


def _copy_chunk(
    client: SearchConsoleClient, prop: SearchProperty, start: date, end: date
) -> dict[str, int]:
    website = prop.website
    totals = client.all_rows(prop.site_url, start=start, end=end, dimensions=["date"])
    pages = client.all_rows(prop.site_url, start=start, end=end, dimensions=["date", "page"])
    queries = client.all_rows(
        prop.site_url, start=start, end=end, dimensions=["date", "query", "page"]
    )
    with transaction.atomic():
        return {
            "days": _store(
                GscDaily, website, totals, lambda k: {"date": _day(k[0])}, ["website", "date"]
            ),
            "pages": _store(
                GscPageDaily,
                website,
                pages,
                lambda k: {"date": _day(k[0]), "page": k[1][:PAGE_MAX]},
                ["website", "date", "page"],
            ),
            "queries": _store(
                GscQueryDaily,
                website,
                queries,
                lambda k: {"date": _day(k[0]), "query": k[1][:QUERY_MAX], "page": k[2][:PAGE_MAX]},
                ["website", "date", "query", "page"],
            ),
        }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def copy_sitemaps(client: SearchConsoleClient, prop: SearchProperty) -> int:
    seen: list[str] = []
    for row in client.list_sitemaps(prop.site_url):
        path = str(row.get("path", ""))
        if not path:
            continue
        seen.append(path)
        GscSitemap.objects.update_or_create(
            website=prop.website,
            path=path,
            defaults={
                "workspace_id": prop.workspace_id,
                "is_index": bool(row.get("isSitemapsIndex")),
                "is_pending": bool(row.get("isPending")),
                "last_submitted": _parse_time(row.get("lastSubmitted")),
                "last_downloaded": _parse_time(row.get("lastDownloaded")),
                "errors": int(row.get("errors", 0) or 0),
                "warnings": int(row.get("warnings", 0) or 0),
                "contents": row.get("contents", []),
            },
        )
    GscSitemap.objects.filter(website=prop.website).exclude(path__in=seen).delete()
    return len(seen)


def start_run(website: Website, *, full: bool = False) -> SyncRun:
    return SyncRun.objects.create(
        workspace_id=website.workspace_id, website=website, full=full, kind="search"
    )


def _copy_all(prop: SearchProperty, run: SyncRun) -> dict[str, Any]:
    client = SearchConsoleClient(prop.connection)  # type: ignore[arg-type]
    start, end = sync_window(prop, full=run.full)
    totals = {"days": 0, "pages": 0, "queries": 0}
    for first, last in chunks(start, end):
        for name, count in _copy_chunk(client, prop, first, last).items():
            totals[name] += count
    sitemaps = copy_sitemaps(client, prop)
    prop.first_date = min(filter(None, [prop.first_date, start]))
    prop.last_date = end
    prop.last_synced_at = timezone.now()
    prop.last_error = ""
    prop.save()
    return {**totals, "sitemaps": sitemaps, "from": start.isoformat(), "to": end.isoformat()}


def execute_run(run: SyncRun) -> SyncRun:
    """Run a queued search sync to the end and record the outcome. Google problems never raise."""
    website = run.website
    prop = (
        SearchProperty.objects.select_related("website", "connection")
        .filter(website=website)
        .first()
    )
    run.status, run.started_at = SyncStatus.RUNNING, timezone.now()
    run.save(update_fields=["status", "started_at", "updated_at"])
    try:
        if prop is None or prop.connection is None:
            raise DomainError("no_search_property")
        with redis_lock(f"content-search:{website.pk}", ttl_seconds=1800):
            run.stats = _copy_all(prop, run)
            run.status = SyncStatus.DONE
    except LockNotAcquiredError:
        run.status, run.error = SyncStatus.FAILED, "another sync of this site is already running"
    except AuthExpiredError:
        run.status, run.error = SyncStatus.FAILED, "google_unauthorised"
    except DomainError as exc:
        log.warning("search_sync_failed", website=str(website.pk), code=exc.code.value)
        run.status, run.error = SyncStatus.FAILED, (str(exc) or exc.code.value)[:500]
    if run.status == SyncStatus.FAILED and prop is not None:
        prop.last_error = run.error[:300]
        prop.save(update_fields=["last_error", "updated_at"])
    run.finished_at = timezone.now()
    run.save()
    return run
