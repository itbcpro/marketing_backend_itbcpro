"""Google PageSpeed Insights runs for a website's pages, kept for comparison over time."""

import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from django.utils import timezone

from content import search_report
from content.google import pagespeed
from content.models import AnalysisStatus, PageSpeedResult, Website
from content.tasks import run_pagespeed
from core.errors import DomainError, ValidationFailedError
from core.logging import get_logger

log = get_logger(__name__)

MAX_URLS = 10
RECENT = timedelta(minutes=10)
STUCK_AFTER = timedelta(minutes=10)
HISTORY = 30


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def same_site(website: Website, url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and _host(url) == _host(website.base_url)


def top_urls(website: Website, count: int = 5) -> list[str]:
    """The pages that matter most: those with the most search clicks, else the home page."""
    window = search_report.resolve_window(website, days=28, compare="none")
    ranked = search_report.rows(website, window, kind="page", limit=count)
    urls = [r["page"] for r in ranked if same_site(website, r["page"])]
    return urls or [website.base_url]


def start(
    website: Website, urls: list[str], strategies: list[str], *, requested_by: str
) -> list[PageSpeedResult]:
    """Queue one run per address and device; a run queued a moment ago is reused."""
    wanted = list(dict.fromkeys(u.strip() for u in urls if u.strip()))[:MAX_URLS]
    if not wanted:
        raise ValidationFailedError("choose at least one page")
    bad = [u for u in wanted if not same_site(website, u)]
    if bad:
        raise ValidationFailedError("only pages of this website can be tested")
    modes = [s for s in dict.fromkeys(strategies) if s in pagespeed.STRATEGIES]
    if not modes:
        raise ValidationFailedError("choose mobile, desktop or both")
    out = []
    for url in wanted:
        for mode in modes:
            existing = PageSpeedResult.objects.filter(
                website=website,
                url=url,
                strategy=mode,
                status__in=[AnalysisStatus.QUEUED, AnalysisStatus.RUNNING],
                created_at__gte=timezone.now() - RECENT,
            ).first()
            if existing:
                out.append(existing)
                continue
            result = PageSpeedResult.objects.create(
                workspace_id=website.workspace_id,
                website=website,
                url=url,
                strategy=mode,
                requested_by=requested_by,
            )
            run_pagespeed.delay(str(result.pk))
            out.append(result)
    return out


def execute(result: PageSpeedResult) -> PageSpeedResult:
    """Run one queued test to the end. Never raises for Google problems."""
    result.status = AnalysisStatus.RUNNING
    result.save(update_fields=["status", "updated_at"])
    try:
        data = pagespeed.run(result.url, result.strategy, workspace_id=result.workspace_id)
    except DomainError as exc:
        log.warning("pagespeed_failed", url=result.url, code=exc.code.value)
        result.status, result.error = AnalysisStatus.FAILED, str(exc)[:300] or exc.code.value
    else:
        result.performance = data["performance"]
        result.seo = data["seo"]
        result.accessibility = data["accessibility"]
        result.best_practices = data["best_practices"]
        result.lab, result.field = data["lab"], data["field"]
        result.opportunities = data["opportunities"]
        result.status = AnalysisStatus.DONE
    result.finished_at = timezone.now()
    result.save()
    return result


def serialize(result: PageSpeedResult, previous: PageSpeedResult | None = None) -> dict[str, Any]:
    stuck = (
        result.status in {AnalysisStatus.QUEUED, AnalysisStatus.RUNNING}
        and timezone.now() - result.created_at > STUCK_AFTER
    )
    out: dict[str, Any] = {
        "id": str(result.pk),
        "url": result.url,
        "strategy": result.strategy,
        "status": AnalysisStatus.FAILED if stuck else result.status,
        "error": "no_worker" if stuck else result.error,
        "performance": result.performance,
        "seo": result.seo,
        "accessibility": result.accessibility,
        "best_practices": result.best_practices,
        "lab": result.lab,
        "field": result.field,
        "opportunities": result.opportunities,
        "tested_at": (result.finished_at or result.created_at).isoformat(),
        "previous": None,
    }
    if previous is not None:
        out["previous"] = {
            "performance": previous.performance,
            "seo": previous.seo,
            "accessibility": previous.accessibility,
            "best_practices": previous.best_practices,
            "lab": previous.lab,
            "tested_at": (previous.finished_at or previous.created_at).isoformat(),
        }
    return out


def latest(website: Website) -> list[dict[str, Any]]:
    """The newest run per address and device, with the run before it for comparison."""
    seen: dict[tuple[str, str], list[PageSpeedResult]] = {}
    for r in PageSpeedResult.objects.filter(website=website).order_by("-created_at")[:400]:
        seen.setdefault((r.url, r.strategy), []).append(r)
    out = []
    for runs in seen.values():
        newest = runs[0]
        before = next(
            (r for r in runs[1:] if r.status == AnalysisStatus.DONE and r.pk != newest.pk), None
        )
        out.append(serialize(newest, before))
    out.sort(key=lambda r: (r["url"], r["strategy"]))
    return out


def history(website: Website, url: str, strategy: str) -> list[dict[str, Any]]:
    rows = PageSpeedResult.objects.filter(
        website=website, url=url, strategy=strategy, status=AnalysisStatus.DONE
    ).order_by("-created_at")[:HISTORY]
    return [serialize(r) for r in reversed(list(rows))]


def get(website_id: uuid.UUID, result_id: uuid.UUID) -> PageSpeedResult | None:
    return PageSpeedResult.objects.filter(pk=result_id, website_id=website_id).first()


def prompt_view(website: Website) -> list[dict[str, Any]]:
    """Short summary for the AI: latest finished score and key numbers per address and device."""
    out = []
    for row in latest(website):
        if row["status"] != AnalysisStatus.DONE:
            continue
        out.append(
            {
                "url": row["url"],
                "device": row["strategy"],
                "performance": row["performance"],
                "seo": row["seo"],
                "lab": row["lab"],
                "real_users": row["field"],
                "biggest_problems": [
                    f"{o['title']} ({o['display']})" if o["display"] else o["title"]
                    for o in row["opportunities"][:3]
                ],
            }
        )
    return out
