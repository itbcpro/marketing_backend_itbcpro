"""Search Console use cases: link a website to a property, and read the copied data."""

import uuid
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

from django.db.models import F, FloatField, Sum

from connections.models import Connection
from content.google.gsc import PROVIDER, SearchConsoleClient
from content.models import (
    GscDaily,
    GscPageDaily,
    GscQueryDaily,
    SearchProperty,
    Website,
)
from core.errors import NotFoundError, ValidationFailedError

UNVERIFIED = "siteUnverifiedUser"
TOP = 100
MAX_DAYS = 480


# --- linking ------------------------------------------------------------------------------


def best_match(website: Website, sites: list[dict[str, str]]) -> str | None:
    """The property that covers the whole site: a domain property first, then a URL prefix."""
    host = (urlsplit(website.base_url).hostname or "").lower().removeprefix("www.")
    usable = {s["site_url"] for s in sites if s["permission"] != UNVERIFIED}
    for candidate in (
        f"sc-domain:{host}",
        f"https://{host}/",
        f"https://www.{host}/",
        f"http://{host}/",
        f"http://www.{host}/",
    ):
        if candidate in usable:
            return candidate
    return None


def candidates(website: Website, connection: Connection) -> dict[str, Any]:
    sites = SearchConsoleClient(connection).list_sites()
    return {"sites": sites, "suggested": best_match(website, sites)}


def google_connection(workspace_id: uuid.UUID, connection_id: uuid.UUID) -> Connection:
    found = Connection.objects.filter(
        pk=connection_id, workspace_id=workspace_id, provider=PROVIDER
    ).first()
    if found is None:
        raise NotFoundError("Google connection not found")
    return found


def link(website: Website, connection: Connection, site_url: str) -> SearchProperty:
    """Link the site to one of the properties the Google account can read, then queue a backfill."""
    sites = {s["site_url"]: s for s in SearchConsoleClient(connection).list_sites()}
    chosen = sites.get(site_url)
    if chosen is None or chosen["permission"] == UNVERIFIED:
        raise ValidationFailedError("this Google account cannot read that property")
    prop, _ = SearchProperty.objects.update_or_create(
        website=website,
        defaults={
            "workspace_id": website.workspace_id,
            "connection": connection,
            "site_url": site_url,
            "permission": chosen["permission"],
            "first_date": None,
            "last_date": None,
            "last_error": "",
        },
    )
    return prop


def unlink(website: Website) -> None:
    SearchProperty.objects.filter(website=website).delete()
    GscDaily.objects.filter(website=website).delete()
    GscPageDaily.objects.filter(website=website).delete()
    GscQueryDaily.objects.filter(website=website).delete()


# --- reading ------------------------------------------------------------------------------


def _ctr(clicks: int, impressions: int) -> float:
    return round(clicks / impressions, 4) if impressions else 0.0


def _position(rows: Any) -> float:
    """Impression-weighted average position (Search Console's own definition)."""
    agg = rows.aggregate(
        w=Sum(F("position") * F("impressions"), output_field=FloatField()), i=Sum("impressions")
    )
    return round((agg["w"] or 0.0) / agg["i"], 1) if agg["i"] else 0.0


def totals(website: Website, start: date, end: date) -> dict[str, Any]:
    rows = GscDaily.objects.filter(website=website, date__range=(start, end))
    agg = rows.aggregate(clicks=Sum("clicks"), impressions=Sum("impressions"))
    clicks, impressions = int(agg["clicks"] or 0), int(agg["impressions"] or 0)
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": _ctr(clicks, impressions),
        "position": _position(rows),
    }


def _delta(now: dict[str, Any], before: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in ("clicks", "impressions", "ctr", "position"):
        out[key] = round(now[key] - before[key], 4)
    return out


def period(days: int, *, last: date | None) -> tuple[date, date, date, date] | None:
    """The current window and the equally long one before it, ending on the last day with data."""
    if last is None:
        return None
    days = max(1, min(days, MAX_DAYS // 2))
    end = last
    start = end - timedelta(days=days - 1)
    return start, end, start - timedelta(days=days), start - timedelta(days=1)


def series(website: Website, start: date, end: date) -> list[dict[str, Any]]:
    rows = GscDaily.objects.filter(website=website, date__range=(start, end)).order_by("date")
    return [
        {
            "date": r.date.isoformat(),
            "clicks": r.clicks,
            "impressions": r.impressions,
            "position": round(r.position, 1),
        }
        for r in rows
    ]


def _grouped(qs: Any, key: str, limit: int) -> list[dict[str, Any]]:
    rows = (
        qs.values(key)
        .annotate(
            total_clicks=Sum("clicks"),
            total_impressions=Sum("impressions"),
            weighted=Sum(F("position") * F("impressions"), output_field=FloatField()),
        )
        .order_by("-total_clicks", "-total_impressions")[:limit]
    )
    out = []
    for r in rows:
        clicks, impressions = int(r["total_clicks"] or 0), int(r["total_impressions"] or 0)
        out.append(
            {
                key: r[key],
                "clicks": clicks,
                "impressions": impressions,
                "ctr": _ctr(clicks, impressions),
                "position": round((r["weighted"] or 0.0) / impressions, 1) if impressions else 0.0,
            }
        )
    return out


def top_queries(
    website: Website, start: date, end: date, *, limit: int = TOP, text: str = ""
) -> list[dict[str, Any]]:
    qs = GscQueryDaily.objects.filter(website=website, date__range=(start, end))
    if text:
        qs = qs.filter(query__icontains=text)
    return _grouped(qs, "query", limit)


def top_pages(
    website: Website, start: date, end: date, *, limit: int = TOP
) -> list[dict[str, Any]]:
    qs = GscPageDaily.objects.filter(website=website, date__range=(start, end))
    return _grouped(qs, "page", limit)


def overview(website: Website, days: int) -> dict[str, Any]:
    prop = SearchProperty.objects.filter(website=website).first()
    window = period(days, last=prop.last_date if prop else None)
    if window is None:
        return {"has_data": False}
    start, end, prev_start, prev_end = window
    now = totals(website, start, end)
    before = totals(website, prev_start, prev_end)
    return {
        "has_data": True,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "totals": now,
        "previous": before,
        "delta": _delta(now, before),
        "series": series(website, start, end),
        "queries": top_queries(website, start, end, limit=20),
        "pages": top_pages(website, start, end, limit=20),
    }


def page_urls(url: str) -> list[str]:
    """The ways Search Console may spell one page's address."""
    bare = url.rstrip("/")
    return list({url, bare, f"{bare}/"})


def page_search(website: Website, url: str, days: int) -> dict[str, Any]:
    prop = SearchProperty.objects.filter(website=website).first()
    window = period(days, last=prop.last_date if prop else None)
    if window is None:
        return {"has_data": False}
    start, end, prev_start, prev_end = window
    urls = page_urls(url)

    def pack(first: date, last: date) -> dict[str, Any]:
        rows = GscPageDaily.objects.filter(
            website=website, page__in=urls, date__range=(first, last)
        )
        agg = rows.aggregate(clicks=Sum("clicks"), impressions=Sum("impressions"))
        clicks, impressions = int(agg["clicks"] or 0), int(agg["impressions"] or 0)
        return {
            "clicks": clicks,
            "impressions": impressions,
            "ctr": _ctr(clicks, impressions),
            "position": _position(rows),
        }

    now, before = pack(start, end), pack(prev_start, prev_end)
    daily = (
        GscPageDaily.objects.filter(website=website, page__in=urls, date__range=(start, end))
        .values("date")
        .annotate(total_clicks=Sum("clicks"), total_impressions=Sum("impressions"))
        .order_by("date")
    )
    queries = _grouped(
        GscQueryDaily.objects.filter(website=website, page__in=urls, date__range=(start, end)),
        "query",
        TOP,
    )
    return {
        "has_data": True,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "totals": now,
        "previous": before,
        "delta": _delta(now, before),
        "series": [
            {
                "date": r["date"].isoformat(),
                "clicks": int(r["total_clicks"]),
                "impressions": int(r["total_impressions"]),
            }
            for r in daily
        ],
        "queries": queries,
    }


def prompt_view(website: Website, url: str) -> dict[str, Any]:
    """What the reviewer is told about a page's search results (measured numbers only)."""
    data = page_search(website, url, 28)
    if not data["has_data"]:
        return {
            "available": False,
            "note": "Search Console is not connected, so there are no search numbers.",
        }
    return {
        "available": True,
        "period": f"{data['from']} to {data['to']}",
        "totals": data["totals"],
        "change_vs_previous_period": data["delta"],
        "top_queries": data["queries"][:15],
    }
