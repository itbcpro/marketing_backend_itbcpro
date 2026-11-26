"""Reads over the inventory: filters, issue flags and the summary shown on the Website screen."""

from typing import Any

from django.db.models import Count, IntegerField, Q, QuerySet, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from content.models import ContentItem, ItemSource, Website

THIN_WORDS = 300
HTTP_ERROR = 400
ISSUE_FILTERS: dict[str, Q] = {
    "no_meta_description": Q(seo__has_key="description", seo__description=""),
    "thin_content": Q(kind="post", stats__word_count__lt=THIN_WORDS),
    "images_missing_alt": Q(stats__images_missing_alt__gt=0),
    "not_in_sitemap": Q(source=ItemSource.WORDPRESS, status="publish", in_sitemap=False),
    "noindex": Q(seo__robots__icontains="noindex"),
    "http_error": Q(http_status__gte=HTTP_ERROR),
}
SORTS = {
    "modified": ("-modified_at", "title"),
    "oldest": ("modified_at", "title"),
    "title": ("title",),
    "words": ("-word_count_sort", "title"),
}


def live_items(website: Website) -> QuerySet[ContentItem]:
    return ContentItem.objects.filter(website=website, removed_at__isnull=True)


def issues_of(item: ContentItem) -> list[str]:
    """Which inventory checks this item fails (cheap, no network); mirrors ``ISSUE_FILTERS``."""
    seo, stats = item.seo or {}, item.stats or {}
    checks = {
        "no_meta_description": "description" in seo and not seo["description"],
        "thin_content": item.kind == "post" and stats.get("word_count", THIN_WORDS) < THIN_WORDS,
        "images_missing_alt": stats.get("images_missing_alt", 0) > 0,
        "not_in_sitemap": (
            item.source == ItemSource.WORDPRESS and item.status == "publish" and not item.in_sitemap
        ),
        "noindex": "noindex" in str(seo.get("robots", "")).lower(),
        "http_error": item.http_status is not None and item.http_status >= HTTP_ERROR,
    }
    return [name for name, failed in checks.items() if failed]


def filtered(
    website: Website,
    *,
    kind: str = "",
    status: str = "",
    issue: str = "",
    q: str = "",
    removed: bool = False,
    sort: str = "modified",
) -> QuerySet[ContentItem]:
    rows = ContentItem.objects.filter(website=website)
    rows = (
        rows.filter(removed_at__isnull=False) if removed else rows.filter(removed_at__isnull=True)
    )
    if kind:
        rows = rows.filter(kind=kind)
    if status:
        rows = rows.filter(status=status)
    if issue in ISSUE_FILTERS:
        rows = rows.filter(ISSUE_FILTERS[issue])
    if q:
        rows = rows.filter(Q(title__icontains=q) | Q(url__icontains=q) | Q(slug__icontains=q))
    rows = rows.annotate(
        word_count_sort=Cast(KeyTextTransform("word_count", "stats"), IntegerField())
    )
    return rows.order_by(*SORTS.get(sort, SORTS["modified"]))


def summary(website: Website) -> dict[str, Any]:
    rows = live_items(website)
    by_kind = dict(rows.values_list("kind").annotate(n=Count("id")).values_list("kind", "n"))
    by_status = dict(rows.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    words = rows.annotate(
        w=Cast(KeyTextTransform("word_count", "stats"), IntegerField())
    ).aggregate(total=Sum("w"))["total"]
    counts = website.profile.get("counts") or {}
    comparison = {}
    for kind, label in (("post", "posts"), ("page", "pages")):
        expected = counts.get(label)
        if expected is None:
            continue
        comparison[kind] = {
            "inventory": by_kind.get(kind, 0),
            "wordpress": expected,
            "match": by_kind.get(kind, 0) == expected,
        }
    return {
        "total": rows.count(),
        "removed": ContentItem.objects.filter(website=website, removed_at__isnull=False).count(),
        "by_kind": by_kind,
        "by_status": by_status,
        "words": int(words or 0),
        "issues": {name: rows.filter(cond).count() for name, cond in ISSUE_FILTERS.items()},
        "wordpress_check": comparison,
    }
