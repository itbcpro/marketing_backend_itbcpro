"""Search Console reports with any period, filters and a comparison period.

Everything here reads the copied data only; nothing calls Google.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.db.models import F, FloatField, Sum

from content.models import GscDaily, GscPageDaily, GscQueryDaily, SearchProperty, Website
from content.search import MAX_DAYS, TOP, _ctr, _delta, _position

MAX_ROWS = 5000
EXPECTED_CTR = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02}
FALLBACK_CTR = 0.015
LAST_PAGE_ONE = 10
LOW_CTR_RATIO = 0.6
STRIKING_FROM, STRIKING_TO = 8.0, 20.0
DROP_RATIO = 0.3
MIN_DROP_CLICKS = 5
MIN_LOST_CLICKS = 3
YEAR_SHIFT_DAYS = 364
SORTS = {
    "clicks",
    "impressions",
    "ctr",
    "position",
    "clicks_change",
    "impressions_change",
    "position_change",
}
COMPARE_MODES = {"previous", "year", "none"}
STATUSES = {"new", "lost", "up", "down", "flat"}


@dataclass(frozen=True)
class Window:
    start: date
    end: date
    prev_start: date | None = None
    prev_end: date | None = None

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def compared(self) -> bool:
        return self.prev_start is not None and self.prev_end is not None


def resolve_window(
    website: Website,
    *,
    days: int = 28,
    start: date | None = None,
    end: date | None = None,
    compare: str = "previous",
) -> Window | None:
    """The period to show (preset length or chosen dates) and the period to compare it with."""
    prop = SearchProperty.objects.filter(website=website).first()
    last = prop.last_date if prop else None
    if last is None:
        return None
    if start and end:
        end = min(end, last)
        start = min(start, end)
        if (end - start).days + 1 > MAX_DAYS:
            start = end - timedelta(days=MAX_DAYS - 1)
    else:
        days = max(1, min(days, MAX_DAYS // 2))
        end = last
        start = end - timedelta(days=days - 1)
    length = (end - start).days + 1
    if compare == "year":
        shift = timedelta(days=YEAR_SHIFT_DAYS)
        return Window(start, end, start - shift, end - shift)
    if compare == "none":
        return Window(start, end)
    prev_end = start - timedelta(days=1)
    return Window(start, end, prev_end - timedelta(days=length - 1), prev_end)


def _scope(website: Website, first: date, last: date, text: str, page: str) -> Any:
    """Rows to total up: the narrowest table that can answer the chosen filters."""
    if text:
        qs = GscQueryDaily.objects.filter(
            website=website, date__range=(first, last), query__icontains=text
        )
        return qs.filter(page__icontains=page) if page else qs
    if page:
        return GscPageDaily.objects.filter(
            website=website, date__range=(first, last), page__icontains=page
        )
    return GscDaily.objects.filter(website=website, date__range=(first, last))


def _pack(rows: Any) -> dict[str, Any]:
    agg = rows.aggregate(clicks=Sum("clicks"), impressions=Sum("impressions"))
    clicks, impressions = int(agg["clicks"] or 0), int(agg["impressions"] or 0)
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": _ctr(clicks, impressions),
        "position": _position(rows),
    }


def _daily(rows: Any) -> list[dict[str, Any]]:
    grouped = (
        rows.values("date")
        .annotate(
            c=Sum("clicks"),
            i=Sum("impressions"),
            w=Sum(F("position") * F("impressions"), output_field=FloatField()),
        )
        .order_by("date")
    )
    return [
        {
            "date": r["date"].isoformat(),
            "clicks": int(r["c"] or 0),
            "impressions": int(r["i"] or 0),
            "position": round((r["w"] or 0.0) / r["i"], 1) if r["i"] else 0.0,
        }
        for r in grouped
    ]


def report(
    website: Website, window: Window | None, *, text: str = "", page: str = ""
) -> dict[str, Any]:
    """Totals, daily series and the comparison for any period and filter."""
    if window is None:
        return {"has_data": False}
    current = _scope(website, window.start, window.end, text, page)
    now = _pack(current)
    out: dict[str, Any] = {
        "has_data": True,
        "from": window.start.isoformat(),
        "to": window.end.isoformat(),
        "totals": now,
        "series": _daily(current),
        "previous": None,
        "delta": None,
        "previous_from": None,
        "previous_to": None,
        "previous_series": [],
    }
    if window.prev_start and window.prev_end:
        rows = _scope(website, window.prev_start, window.prev_end, text, page)
        before = _pack(rows)
        out.update(
            previous=before,
            delta=_delta(now, before),
            previous_from=window.prev_start.isoformat(),
            previous_to=window.prev_end.isoformat(),
            previous_series=_daily(rows),
        )
    return out


def _stats(qs: Any, key: str) -> dict[str, dict[str, Any]]:
    grouped = qs.values(key).annotate(
        c=Sum("clicks"),
        i=Sum("impressions"),
        w=Sum(F("position") * F("impressions"), output_field=FloatField()),
    )[:MAX_ROWS]
    out: dict[str, dict[str, Any]] = {}
    for r in grouped:
        clicks, impressions = int(r["c"] or 0), int(r["i"] or 0)
        out[r[key]] = {
            "clicks": clicks,
            "impressions": impressions,
            "ctr": _ctr(clicks, impressions),
            "position": round((r["w"] or 0.0) / impressions, 1) if impressions else 0.0,
        }
    return out


def _table(
    website: Website, kind: str, first: date, last: date, text: str, page: str
) -> tuple[Any, str]:
    if kind == "page" and not text:
        pages = GscPageDaily.objects.filter(website=website, date__range=(first, last))
        return (pages.filter(page__icontains=page) if page else pages), "page"
    queries = GscQueryDaily.objects.filter(website=website, date__range=(first, last))
    if text:
        queries = queries.filter(query__icontains=text)
    if page:
        queries = queries.filter(page__icontains=page)
    return queries, ("page" if kind == "page" else "query")


def _status(row: dict[str, Any], compared: bool) -> str:
    if not compared:
        return ""
    if not row["prev_impressions"]:
        return "new"
    if not row["impressions"]:
        return "lost"
    change = row["clicks"] - row["prev_clicks"]
    if change > 0:
        return "up"
    return "down" if change < 0 else "flat"


def _merge(key: str, name: str, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {
        key: name,
        **a,
        "prev_clicks": b["clicks"],
        "prev_impressions": b["impressions"],
        "prev_ctr": b["ctr"],
        "prev_position": b["position"],
        "d_clicks": a["clicks"] - b["clicks"],
        "d_impressions": a["impressions"] - b["impressions"],
        "d_ctr": round(a["ctr"] - b["ctr"], 4),
        "d_position": (
            round(a["position"] - b["position"], 1) if a["position"] and b["position"] else 0.0
        ),
    }


def _sort(out: list[dict[str, Any]], sort: str) -> None:
    if sort == "clicks_change":
        out.sort(key=lambda r: r["d_clicks"])
    elif sort == "impressions_change":
        out.sort(key=lambda r: r["d_impressions"])
    elif sort == "position_change":
        out.sort(key=lambda r: -r["d_position"])
    elif sort == "position":
        out.sort(key=lambda r: r["position"] or 999)
    else:
        out.sort(key=lambda r: (-r[sort], -r["impressions"]))


def rows(
    website: Website,
    window: Window | None,
    *,
    kind: str,
    text: str = "",
    page: str = "",
    sort: str = "clicks",
    min_impressions: int = 0,
    status: str = "",
    limit: int = TOP,
) -> list[dict[str, Any]]:
    """Queries or pages with this period's numbers, the comparison period's and the change."""
    if window is None:
        return []
    qs, key = _table(website, kind, window.start, window.end, text, page)
    now = _stats(qs, key)
    before: dict[str, dict[str, Any]] = {}
    if window.prev_start and window.prev_end:
        pqs, _ = _table(website, kind, window.prev_start, window.prev_end, text, page)
        before = _stats(pqs, key)
    zero = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    out = []
    for name in set(now) | set(before):
        row = _merge(key, name, now.get(name, zero), before.get(name, zero))
        row["status"] = _status(row, window.compared)
        if status and row["status"] != status:
            continue
        if max(row["impressions"], row["prev_impressions"]) < min_impressions:
            continue
        out.append(row)
    _sort(out, sort if sort in SORTS else "clicks")
    return out[:limit]


def _expected_ctr(position: float) -> float:
    return EXPECTED_CTR.get(round(position), FALLBACK_CTR)


def opportunities(website: Website, window: Window | None, *, limit: int = 15) -> dict[str, Any]:
    """Where a small change is most likely to bring clicks, found from the measured numbers."""
    if window is None:
        return {"has_data": False}
    scale = max(1, window.days // 7)
    queries = rows(website, window, kind="query", limit=MAX_ROWS)
    pages = rows(website, window, kind="page", limit=MAX_ROWS)
    low_ctr = [
        r
        for r in queries
        if r["position"]
        and r["position"] < LAST_PAGE_ONE
        and r["impressions"] >= 40 * scale
        and r["ctr"] < _expected_ctr(r["position"]) * LOW_CTR_RATIO
    ]
    striking = [
        r
        for r in queries
        if STRIKING_FROM <= r["position"] <= STRIKING_TO and r["impressions"] >= 10 * scale
    ]
    drops = [
        r
        for r in pages
        if r["clicks"] > 0
        and r["prev_clicks"] >= MIN_DROP_CLICKS
        and r["clicks"] <= r["prev_clicks"] * (1 - DROP_RATIO)
    ]
    lost = [r for r in pages if r["prev_clicks"] >= MIN_LOST_CLICKS and r["clicks"] == 0]
    low_ctr.sort(key=lambda r: -r["impressions"])
    striking.sort(key=lambda r: -r["impressions"])
    drops.sort(key=lambda r: r["d_clicks"])
    return {
        "has_data": True,
        "low_ctr_queries": low_ctr[:limit],
        "striking_distance_queries": striking[:limit],
        "declining_pages": drops[:limit],
        "lost_pages": lost[:limit],
    }
