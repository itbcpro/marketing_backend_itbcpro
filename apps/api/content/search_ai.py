"""AI analysis of a website's search results: problems, pages to change, quick wins.

The model only sees measured Search Console numbers (the period, the comparison period and the
stored copy of each page) and cannot change anything. Addresses in the answer that are not in the
data are dropped, so a suggestion always points at a real page.
"""

import uuid
from datetime import timedelta
from typing import Any, Literal

from django.utils import timezone
from pydantic import BaseModel, Field

from content import ai, search_report, speed
from content.models import AnalysisStatus, ContentItem, SearchAnalysis, Website
from content.search_report import Window
from content.tasks import analyze_search
from core.errors import DomainError, ValidationFailedError
from core.logging import get_logger

log = get_logger(__name__)

PROMPT_KEY = "content/search-analysis"
MAX_INSTRUCTION = 500
PAGES_SHOWN = 40
QUERIES_SHOWN = 40
MOVERS_SHOWN = 15
STUCK_AFTER = timedelta(minutes=10)
Level = Literal["high", "medium", "low"]


class SearchProblem(BaseModel):
    severity: Level
    area: str
    evidence: str
    fix: str


class PageToUpdate(BaseModel):
    url: str
    priority: Level
    why: str
    what_to_change: list[str] = Field(min_length=1, max_length=6)
    target_queries: list[str] = Field(max_length=6)


class QuickWin(BaseModel):
    action: str
    url: str
    expected_effect: str


class NewContent(BaseModel):
    topic: str
    query: str
    why: str


class Cannibalization(BaseModel):
    query: str
    urls: list[str] = Field(min_length=2, max_length=5)
    advice: str


class SearchReview(BaseModel):
    summary: str
    verdict: Literal["growing", "stable", "declining", "mixed"]
    what_changed: list[str] = Field(max_length=8)
    progress_since_last: str
    problems: list[SearchProblem] = Field(max_length=10)
    pages_to_update: list[PageToUpdate] = Field(max_length=12)
    quick_wins: list[QuickWin] = Field(max_length=8)
    new_content: list[NewContent] = Field(max_length=6)
    cannibalization: list[Cannibalization] = Field(max_length=5)
    speed_notes: list[str] = Field(max_length=5)
    next_steps: list[str] = Field(max_length=6)


def start(
    website: Website,
    window: Window,
    *,
    compare: str,
    instruction: str,
    requested_by: str,
) -> SearchAnalysis:
    if len(instruction) > MAX_INSTRUCTION:
        raise ValidationFailedError("the request is too long")
    busy = SearchAnalysis.objects.filter(
        website=website,
        status__in=[AnalysisStatus.QUEUED, AnalysisStatus.RUNNING],
        created_at__gte=timezone.now() - STUCK_AFTER,
    ).first()
    if busy:
        return busy
    analysis = SearchAnalysis.objects.create(
        workspace_id=website.workspace_id,
        website=website,
        period_start=window.start,
        period_end=window.end,
        compare=compare,
        instruction=instruction.strip(),
        requested_by=requested_by,
    )
    analyze_search.delay(str(analysis.pk))
    return analysis


def window_of(analysis: SearchAnalysis) -> Window:
    """The analysed period and the comparison it was asked for, rebuilt from the stored dates."""
    start, end = analysis.period_start, analysis.period_end
    length = (end - start).days + 1
    if analysis.compare == "none":
        return Window(start, end)
    if analysis.compare == "year":
        shift = timedelta(days=search_report.YEAR_SHIFT_DAYS)
        return Window(start, end, start - shift, end - shift)
    prev_end = start - timedelta(days=1)
    return Window(start, end, prev_end - timedelta(days=length - 1), prev_end)


def _urls(url: str) -> list[str]:
    bare = url.rstrip("/")
    return list({url, bare, f"{bare}/"})


def _inventory(website: Website, pages: list[str]) -> dict[str, dict[str, Any]]:
    wanted = {u for page in pages for u in _urls(page)}
    out: dict[str, dict[str, Any]] = {}
    for item in ContentItem.objects.filter(
        website=website, url__in=wanted, removed_at__isnull=True
    ):
        seo = item.seo or {}
        info = {
            "title": item.title,
            "seo_title": seo.get("title", ""),
            "meta_description": seo.get("description", ""),
            "words": (item.stats or {}).get("word_count"),
            "last_modified": item.modified_at.date().isoformat() if item.modified_at else None,
            "kind": item.kind,
        }
        for u in _urls(item.url):
            out[u] = info
    return out


def _slim(row: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        key: row[key],
        "clicks": row["clicks"],
        "impressions": row["impressions"],
        "ctr": row["ctr"],
        "position": row["position"],
        "clicks_before": row["prev_clicks"],
        "impressions_before": row["prev_impressions"],
        "position_before": row["prev_position"],
    }


def build_context(website: Website, window: Window) -> dict[str, Any]:
    pages = search_report.rows(website, window, kind="page", limit=PAGES_SHOWN)
    queries = search_report.rows(website, window, kind="query", limit=QUERIES_SHOWN)
    losers = search_report.rows(
        website, window, kind="page", sort="clicks_change", limit=MOVERS_SHOWN
    )
    winners = list(
        reversed(
            search_report.rows(
                website, window, kind="page", sort="clicks_change", limit=search_report.MAX_ROWS
            )[-MOVERS_SHOWN:]
        )
    )
    new_queries = search_report.rows(
        website, window, kind="query", status="new", min_impressions=10, limit=MOVERS_SHOWN
    )
    lost_queries = search_report.rows(
        website, window, kind="query", status="lost", min_impressions=10, limit=MOVERS_SHOWN
    )
    shown = {r["page"] for r in pages + losers + winners}
    inventory = _inventory(website, sorted(shown))
    return {
        "report": search_report.report(website, window),
        "pages": [{**_slim(r, "page"), "stored_copy": inventory.get(r["page"])} for r in pages],
        "queries": [_slim(r, "query") for r in queries],
        "biggest_losers": [_slim(r, "page") for r in losers if r["d_clicks"] < 0],
        "biggest_winners": [_slim(r, "page") for r in winners if r["d_clicks"] > 0],
        "new_queries": [_slim(r, "query") for r in new_queries],
        "lost_queries": [_slim(r, "query") for r in lost_queries],
        "opportunities": search_report.opportunities(website, window, limit=10),
        "known_pages": sorted(shown),
        "speed": speed.prompt_view(website),
    }


def _previous(analysis: SearchAnalysis) -> dict[str, Any]:
    before = (
        SearchAnalysis.objects.filter(
            website=analysis.website, status=AnalysisStatus.DONE, created_at__lt=analysis.created_at
        )
        .order_by("-created_at")
        .first()
    )
    if before is None:
        return {"available": False}
    result = before.result or {}
    return {
        "available": True,
        "analysed_on": before.created_at.date().isoformat(),
        "period": f"{before.period_start} to {before.period_end}",
        "verdict": result.get("verdict"),
        "summary": result.get("summary"),
        "pages_it_recommended_changing": [
            {"url": p["url"], "why": p["why"]} for p in result.get("pages_to_update", [])
        ],
    }


def _clean(review: SearchReview, known: set[str]) -> dict[str, Any]:
    data = review.model_dump(mode="json")
    data["pages_to_update"] = [p for p in data["pages_to_update"] if p["url"] in known]
    data["quick_wins"] = [q for q in data["quick_wins"] if not q["url"] or q["url"] in known]
    data["cannibalization"] = [
        {**c, "urls": [u for u in c["urls"] if u in known]} for c in data["cannibalization"]
    ]
    data["cannibalization"] = [c for c in data["cannibalization"] if len(c["urls"]) >= 2]  # noqa: PLR2004
    return data


def run(analysis: SearchAnalysis) -> SearchAnalysis:
    """Run a queued analysis to the end. Never raises for model or data problems."""
    website = analysis.website
    analysis.status = AnalysisStatus.RUNNING
    analysis.started_at = timezone.now()
    analysis.save(update_fields=["status", "started_at", "updated_at"])
    window = window_of(analysis)
    context = build_context(website, window)
    try:
        review = ai.run_prompt(
            website.workspace_id,
            PROMPT_KEY,
            SearchReview,
            memory_query=f"seo search performance {website.name}",
            user_message="Analyse the search results.",
            site=ai.dumps({"name": website.name, "address": website.base_url}),
            period=ai.dumps(
                {
                    "from": window.start.isoformat(),
                    "to": window.end.isoformat(),
                    "compared_with": (
                        f"{window.prev_start} to {window.prev_end}" if window.compared else "none"
                    ),
                }
            ),
            data=ai.untrusted("search console data", context),
            previous=ai.dumps(_previous(analysis)),
            instruction=analysis.instruction,
        )
    except DomainError as exc:
        log.warning("search_analysis_failed", website=str(website.pk), code=exc.code.value)
        analysis.status, analysis.error = AnalysisStatus.FAILED, exc.code.value
    else:
        analysis.result = _clean(review, set(context["known_pages"]))
        analysis.status = AnalysisStatus.DONE
    analysis.finished_at = timezone.now()
    analysis.save()
    return analysis


def serialize(analysis: SearchAnalysis) -> dict[str, Any]:
    stuck = (
        analysis.status in {AnalysisStatus.QUEUED, AnalysisStatus.RUNNING}
        and timezone.now() - analysis.created_at > STUCK_AFTER
    )
    return {
        "id": str(analysis.pk),
        "status": AnalysisStatus.FAILED if stuck else analysis.status,
        "error": "provider_unavailable" if stuck else analysis.error,
        "period_start": analysis.period_start.isoformat(),
        "period_end": analysis.period_end.isoformat(),
        "compare": analysis.compare,
        "instruction": analysis.instruction,
        "started_at": (analysis.started_at or analysis.created_at).isoformat(),
        "created_at": analysis.created_at.isoformat(),
        "result": analysis.result or None,
    }


def get(website_id: uuid.UUID, analysis_id: uuid.UUID) -> SearchAnalysis | None:
    return SearchAnalysis.objects.filter(pk=analysis_id, website_id=website_id).first()
