"""What the blog steps tell the model about the site: existing posts, link targets, search data."""

import re
from typing import Any
from urllib.parse import urlsplit

from content import review, search
from content.models import ContentItem, SearchProperty, Website

POST_LIMIT = 150
QUERY_LIMIT = 40
NEIGHBOR_LIMIT = 30
WORD = re.compile(r"[a-z0-9]{3,}")


def existing_posts(website: Website, limit: int = POST_LIMIT) -> list[dict[str, Any]]:
    rows = ContentItem.objects.filter(
        website=website, kind="post", status="publish", removed_at__isnull=True
    ).order_by("-published_at")[:limit]
    return [
        {
            "title": r.title,
            "address": r.url,
            "categories": (r.terms or {}).get("categories", []),
            "tags": (r.terms or {}).get("tags", []),
            "words": (r.stats or {}).get("word_count", 0),
            "published": r.published_at.date().isoformat() if r.published_at else "",
        }
        for r in rows
    ]


def search_evidence(website: Website, days: int = 90) -> dict[str, Any]:
    """Measured Search Console queries, or a note that there are none."""
    prop = SearchProperty.objects.filter(website=website).first()
    window = search.period(days, last=prop.last_date if prop else None)
    if window is None:
        return {
            "available": False,
            "note": "Search Console is not connected, so there is no search data.",
        }
    start, end, _, _ = window
    return {
        "available": True,
        "period": f"{start.isoformat()} to {end.isoformat()}",
        "queries": search.top_queries(website, start, end, limit=QUERY_LIMIT),
    }


def neighbors(website: Website, text: str, limit: int = NEIGHBOR_LIMIT) -> list[dict[str, str]]:
    """Live pages the article may link to, ranked by words shared with the topic."""
    wanted = set(WORD.findall(text.lower()))
    rows = list(
        ContentItem.objects.filter(
            website=website, removed_at__isnull=True, status="publish", kind__in=["post", "page"]
        ).exclude(url="")
    )
    scored = sorted(
        rows,
        key=lambda r: -len(wanted & set(WORD.findall(f"{r.title} {r.slug}".lower()))),
    )
    return [{"title": r.title, "url": r.url, "kind": r.kind} for r in scored[:limit]]


def domain_of(website: Website) -> str:
    return urlsplit(website.base_url).netloc.lower()


def site_view(website: Website) -> dict[str, Any]:
    view = review.site_view(website)
    view["brief"] = website.brief or {}
    return view
