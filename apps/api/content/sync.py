"""Inventory sync: keep a local copy of every public page of a website.

WordPress sites are read through the REST API (full content, SEO plugin data, authors, terms);
Git-built sites are read through their sitemap and the pages themselves. Both end up as
``ContentItem`` rows. A sync only reads from the site.
"""

import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from django.db import transaction
from django.utils import timezone

from content import net, normalize, probe, sitemap
from content.models import ContentItem, ItemSource, SyncRun, SyncStatus, Website, WebsiteKind
from content.wordpress import factory
from content.wordpress.client import WordPressClient
from core.errors import DomainError
from core.locks import LockNotAcquiredError, redis_lock
from core.logging import get_logger

log = get_logger(__name__)

sleeper = time.sleep
PAGE_PAUSE_SECONDS = 0.3
MAX_PAGES_PER_RUN = 300
INCREMENTAL_MARGIN = timedelta(hours=1)
HTTP_OK = 200
FIELDS_THAT_CHANGE = ("title", "url", "status", "slug", "modified_at")
EMPTY_FACTS: dict[str, Any] = {"text": "", "outline": [], "images": [], "stats": {}}


def parse_gmt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _upsert(website: Website, key: str, now: datetime, **fields: Any) -> str:
    """Create or update one item; returns ``created``, ``updated`` or ``unchanged``."""
    item = ContentItem.objects.filter(website=website, key=key).first()
    if item is None:
        ContentItem.objects.create(
            workspace_id=website.workspace_id,
            website=website,
            key=key,
            last_seen_at=now,
            changed_at=now,
            **fields,
        )
        return "created"
    changed = item.content_hash != fields.get("content_hash", item.content_hash) or any(
        getattr(item, name) != fields[name] for name in FIELDS_THAT_CHANGE if name in fields
    )
    for name, value in fields.items():
        setattr(item, name, value)
    item.last_seen_at = now
    item.removed_at = None
    if changed:
        item.changed_at = now
    item.save()
    return "updated" if changed else "unchanged"


# --- WordPress ----------------------------------------------------------------------------


def wp_seo(row: dict[str, Any]) -> dict[str, Any]:
    yoast = row.get("yoast_head_json") or {}
    if not isinstance(yoast, dict) or not yoast:
        return {}
    robots = yoast.get("robots") or {}
    return {
        "title": yoast.get("title", ""),
        "description": yoast.get("description", ""),
        "canonical": yoast.get("canonical", ""),
        "robots": f"{robots.get('index', 'index')},{robots.get('follow', 'follow')}",
        "og_title": yoast.get("og_title", ""),
        "plugin": "yoast",
    }


def _wp_fields(
    website: Website, row: dict[str, Any], names: dict[str, dict[int, str]]
) -> dict[str, Any]:
    rendered = (row.get("content") or {}).get("rendered", "")
    title = " ".join(str((row.get("title") or {}).get("rendered", "")).split())
    facts = normalize.normalize_html(rendered, website.base_url)
    language = str(row.get("lang") or website.profile.get("settings", {}).get("language", ""))
    return {
        "source": ItemSource.WORDPRESS,
        "kind": row.get("type", "post"),
        "external_id": str(row["id"]),
        "url": row.get("link", ""),
        "slug": row.get("slug", ""),
        "title": title[:500],
        "status": row.get("status", "publish"),
        "language": language.split("_", maxsplit=1)[0][:20],
        "author": names["users"].get(int(row.get("author") or 0), ""),
        "terms": {
            "categories": [names["categories"].get(i, "") for i in row.get("categories", [])],
            "tags": [names["tags"].get(i, "") for i in row.get("tags", [])],
        },
        "published_at": parse_gmt(row.get("date_gmt")),
        "modified_at": parse_gmt(row.get("modified_gmt")),
        "seo": wp_seo(row),
        "text": facts["text"],
        "outline": facts["outline"],
        "images": facts["images"],
        "stats": facts["stats"],
        "content_hash": normalize.content_hash(title, facts["text"]),
    }


def _since(website: Website, full: bool) -> str:
    """Only ask WordPress for what changed since the last finished sync (with a safety margin)."""
    if full:
        return ""
    last = (
        SyncRun.objects.filter(website=website, kind="inventory", status=SyncStatus.DONE)
        .exclude(started_at=None)
        .order_by("-started_at")
        .first()
    )
    if last is None or last.started_at is None:
        return ""
    return (last.started_at - INCREMENTAL_MARGIN).strftime("%Y-%m-%dT%H:%M:%S")


def _sync_wp_type(
    website: Website,
    client: WordPressClient,
    post_type: str,
    rest_base: str,
    since: str,
    names: dict[str, dict[int, str]],
    now: datetime,
) -> Counter[str]:
    counts: Counter[str] = Counter()
    live_ids = {str(i) for i in client.list_ids(rest_base)}
    page, pages = 1, 1
    while page <= pages:
        rows, pages = client.posts_page(rest_base, page, modified_after=since)
        for row in rows:
            fields = _wp_fields(website, row, names)
            fields["kind"] = post_type
            counts[_upsert(website, f"wp:{post_type}:{row['id']}", now, **fields)] += 1
        page += 1
    scope = ContentItem.objects.filter(website=website, source=ItemSource.WORDPRESS, kind=post_type)
    counts["removed"] = (
        scope.filter(removed_at__isnull=True)
        .exclude(external_id__in=live_ids)
        .update(removed_at=now)
    )
    # Items the incremental read skipped are still alive: keep them current.
    scope.filter(external_id__in=live_ids, last_seen_at__lt=now).update(
        last_seen_at=now, removed_at=None
    )
    return counts


def _mark_sitemap(
    website: Website, entries: list[sitemap.SitemapEntry], now: datetime
) -> dict[str, int]:
    """Flag which items the sitemap lists, and record sitemap URLs no item owns."""
    listed = {sitemap.canon(e.url): e for e in entries}
    owned: set[str] = set()
    items = ContentItem.objects.filter(website=website, removed_at__isnull=True).exclude(
        source=ItemSource.SITEMAP
    )
    for item in items:
        match = bool(item.url) and sitemap.canon(item.url) in listed
        if item.in_sitemap != match:
            item.in_sitemap = match
            item.save(update_fields=["in_sitemap", "updated_at"])
        if match:
            owned.add(sitemap.canon(item.url))
    extra = 0
    for address, entry in listed.items():
        if address in owned:
            continue
        extra += 1
        ContentItem.objects.update_or_create(
            website=website,
            key=f"url:{address}",
            defaults={
                "workspace_id": website.workspace_id,
                "source": ItemSource.SITEMAP,
                "kind": "other",
                "url": entry.url,
                "in_sitemap": True,
                "modified_at": parse_gmt(entry.lastmod),
                "last_seen_at": now,
                "removed_at": None,
            },
        )
    ContentItem.objects.filter(
        website=website, source=ItemSource.SITEMAP, kind="other", last_seen_at__lt=now
    ).update(removed_at=now)
    return {"sitemap_urls": len(listed), "sitemap_only": extra}


def sync_wordpress(website: Website, run: SyncRun, now: datetime) -> dict[str, Any]:
    if website.connection is None:
        raise DomainError("this website has no WordPress connection")
    client = factory.for_connection(website.connection)
    names = {
        "categories": client.names("categories"),
        "tags": client.names("tags"),
        "users": client.names("users"),
    }
    since = _since(website, run.full)
    stats: dict[str, Any] = {"by_type": {}, "incremental": bool(since)}
    total: Counter[str] = Counter()
    for post_type, rest_base in client.post_types():
        counts = _sync_wp_type(website, client, post_type, rest_base, since, names, now)
        stats["by_type"][post_type] = dict(counts)
        total.update(counts)
    stats.update(total)
    stats.update(_mark_sitemap(website, sitemap.collect(website.base_url), now))
    return stats


def rest_base_for(client: WordPressClient, item: ContentItem) -> str:
    """The REST collection of the item's content type."""
    for post_type, base in client.post_types():
        if post_type == item.kind:
            return str(base)
    return {"post": "posts", "page": "pages"}.get(item.kind, item.kind)


def refresh_item(item: ContentItem) -> ContentItem:
    """Re-read one WordPress page from the site (content, headings, images, SEO fields)."""
    website = item.website
    if item.source != ItemSource.WORDPRESS or website.connection is None or not item.external_id:
        raise DomainError("this page is not read through WordPress")
    client = factory.for_connection(website.connection)
    row = client.item_full(rest_base_for(client, item), int(item.external_id))
    if not row:
        raise DomainError("page_not_found")
    fresh = _wp_fields(website, row, {"users": {}, "categories": {}, "tags": {}})
    known = {i["src"]: i["attachment_id"] for i in item.images if i.get("attachment_id")}
    for image in fresh["images"]:  # ids found by file name are kept when the page is read again
        if not image.get("attachment_id"):
            image["attachment_id"] = known.get(image["src"])
    for name in (
        "title", "slug", "url", "status", "modified_at", "text", "outline", "images", "stats",
        "content_hash",
    ):  # fmt: skip
        setattr(item, name, fresh[name])
    if fresh["seo"]:
        item.seo = fresh["seo"]
    item.last_seen_at = timezone.now()
    item.save()
    return item


# --- Git-built (public) sites -------------------------------------------------------------


def _fetch_page(url: str) -> tuple[int | None, str]:
    try:
        response = net.fetch(url, max_bytes=3_000_000)
    except DomainError, httpx.HTTPError:
        return None, ""
    return response.status_code, response.text


def _public_fields(entry: sitemap.SitemapEntry) -> dict[str, Any]:
    status, html = _fetch_page(entry.url)
    facts = normalize.normalize_html(html, entry.url) if html else EMPTY_FACTS
    page = probe.page_facts(html) if html else {}
    title = str(page.get("title", ""))[:500]
    return {
        "source": ItemSource.SITEMAP,
        "kind": "page",
        "url": entry.url,
        "title": title,
        "status": "publish" if status == HTTP_OK else "error",
        "language": str(page.get("lang", ""))[:20],
        "modified_at": parse_gmt(entry.lastmod),
        "seo": {
            "title": title,
            "description": page.get("description", ""),
            "canonical": page.get("canonical", ""),
            "robots": page.get("robots_meta", ""),
        },
        "text": facts["text"],
        "outline": facts["outline"],
        "images": facts["images"],
        "stats": {**facts["stats"], "h1_count": page.get("h1_count", 0)},
        "content_hash": normalize.content_hash(title, facts["text"]),
        "in_sitemap": True,
        "http_status": status,
    }


def sync_public(website: Website, run: SyncRun, now: datetime) -> dict[str, Any]:
    entries = sitemap.collect(website.base_url)
    counts: Counter[str] = Counter()
    known = {
        i.key: i
        for i in ContentItem.objects.filter(website=website, source=ItemSource.SITEMAP, kind="page")
    }
    fetched = 0
    for entry in entries:
        key = f"url:{sitemap.canon(entry.url)}"
        before = known.get(key)
        modified = parse_gmt(entry.lastmod)
        current = (
            before is not None
            and not run.full
            and modified is not None
            and before.modified_at == modified
            and before.http_status == HTTP_OK
        )
        if current or fetched >= MAX_PAGES_PER_RUN:
            if before is not None:
                before.last_seen_at, before.removed_at, before.in_sitemap = now, None, True
                before.save(
                    update_fields=["last_seen_at", "removed_at", "in_sitemap", "updated_at"]
                )
                counts["unchanged"] += 1
            continue
        sleeper(PAGE_PAUSE_SECONDS)
        fetched += 1
        counts[_upsert(website, key, now, **_public_fields(entry))] += 1
    listed = {f"url:{sitemap.canon(e.url)}" for e in entries}
    counts["removed"] = (
        ContentItem.objects.filter(website=website, source=ItemSource.SITEMAP, removed_at=None)
        .exclude(key__in=listed)
        .update(removed_at=now)
    )
    return {**counts, "sitemap_urls": len(entries), "fetched": fetched}


# --- entry points -------------------------------------------------------------------------


def start_run(website: Website, *, full: bool = False) -> SyncRun:
    return SyncRun.objects.create(workspace_id=website.workspace_id, website=website, full=full)


def execute_run(run: SyncRun) -> SyncRun:
    """Run a queued sync to the end and record the outcome. Never raises for site problems."""
    website = run.website
    run.status, run.started_at = SyncStatus.RUNNING, timezone.now()
    run.save(update_fields=["status", "started_at", "updated_at"])
    try:
        with redis_lock(f"content-sync:{website.pk}", ttl_seconds=900):
            now = timezone.now()
            with transaction.atomic():
                if website.kind == WebsiteKind.WORDPRESS:
                    run.stats = sync_wordpress(website, run, now)
                else:
                    run.stats = sync_public(website, run, now)
            run.status = SyncStatus.DONE
    except LockNotAcquiredError:
        run.status, run.error = SyncStatus.FAILED, "another sync of this site is already running"
    except DomainError as exc:
        log.warning("content_sync_failed", website=str(website.pk), code=exc.code.value)
        run.status, run.error = SyncStatus.FAILED, (str(exc) or exc.code.value)[:500]
    run.finished_at = timezone.now()
    run.save()
    return run
