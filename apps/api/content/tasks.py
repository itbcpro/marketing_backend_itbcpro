"""Scheduled website work."""

from core.logging import get_logger
from core.tasks import ReliableTask, reliable_task

log = get_logger(__name__)


@reliable_task("content.probe_website", queue="low", time_limit=180)
def probe_website(self: ReliableTask, website_id: str) -> str:
    from content import services
    from content.models import Website

    website = Website.objects.filter(pk=website_id).first()
    if website is None:
        return "gone"
    return str(services.run_probe(website).status)


@reliable_task("content.probe_all", queue="low", time_limit=600)
def probe_all(self: ReliableTask) -> int:
    """Fan out one probe per website; one slow site never blocks the others."""
    from content.models import Website

    ids = [str(pk) for pk in Website.objects.values_list("pk", flat=True)]
    for website_id in ids:
        probe_website.delay(website_id)
    return len(ids)


@reliable_task("content.sync_website", queue="low", time_limit=1500, soft_time_limit=1440)
def sync_website(self: ReliableTask, run_id: str) -> str:
    """Run one queued inventory sync."""
    from content import sync
    from content.models import SyncRun

    run = SyncRun.objects.select_related("website__connection").filter(pk=run_id).first()
    if run is None:
        return "gone"
    return str(sync.execute_run(run).status)


@reliable_task("content.sync_all", queue="low", time_limit=300)
def sync_all(self: ReliableTask) -> int:
    """Queue an incremental sync for every website."""
    from content import sync
    from content.models import Website

    count = 0
    for website in Website.objects.all():
        run = sync.start_run(website)
        sync_website.delay(str(run.pk))
        count += 1
    return count


@reliable_task("content.improve_post", queue="agents", time_limit=600, soft_time_limit=570)
def improve_post(self: ReliableTask, revision_id: str) -> str:
    """Rewrite the body of one post with AI and keep the result for review."""
    from content import improve
    from content.models import PostRevision

    revision = (
        PostRevision.objects.select_related("item__website__connection")
        .filter(pk=revision_id)
        .first()
    )
    if revision is None:
        return "gone"
    return str(improve.run(revision).status)


@reliable_task("content.analyze_page", queue="agents", time_limit=300, soft_time_limit=270)
def analyze_page(self: ReliableTask, analysis_id: str) -> str:
    """Run one queued AI review of a page."""
    from content import review
    from content.models import PageAnalysis

    analysis = PageAnalysis.objects.select_related("item__website").filter(pk=analysis_id).first()
    if analysis is None:
        return "gone"
    return str(review.run_analysis(analysis).status)


@reliable_task("content.sync_search", queue="low", time_limit=1800, soft_time_limit=1740)
def sync_search(self: ReliableTask, run_id: str) -> str:
    """Copy Search Console data for one website."""
    from content import gsc_sync
    from content.models import SyncRun

    run = SyncRun.objects.select_related("website").filter(pk=run_id, kind="search").first()
    if run is None:
        return "gone"
    return str(gsc_sync.execute_run(run).status)


@reliable_task("content.search_sync_all", queue="low", time_limit=300)
def search_sync_all(self: ReliableTask) -> int:
    """Queue an incremental Search Console sync for every linked website."""
    from content import gsc_sync
    from content.models import SearchProperty

    count = 0
    for prop in SearchProperty.objects.select_related("website"):
        run = gsc_sync.start_run(prop.website)
        sync_search.delay(str(run.pk))
        count += 1
    return count


@reliable_task("content.analyze_search", queue="agents", time_limit=300, soft_time_limit=270)
def analyze_search(self: ReliableTask, analysis_id: str) -> str:
    """Run one queued AI analysis of a website's search results."""
    from content import search_ai
    from content.models import SearchAnalysis

    analysis = SearchAnalysis.objects.select_related("website").filter(pk=analysis_id).first()
    if analysis is None:
        return "gone"
    return str(search_ai.run(analysis).status)


@reliable_task("content.run_pagespeed", queue="low", time_limit=240, soft_time_limit=200)
def run_pagespeed(self: ReliableTask, result_id: str) -> str:
    """Run one Google PageSpeed Insights test."""
    from content import speed
    from content.models import PageSpeedResult

    result = PageSpeedResult.objects.filter(pk=result_id).first()
    if result is None:
        return "gone"
    return str(speed.execute(result).status)


@reliable_task("content.write_article", queue="agents", time_limit=420, soft_time_limit=390)
def write_article(self: ReliableTask, article_id: str) -> str:
    """Do the next step of an article and queue the following one until it is finished."""
    from content.blog import writer
    from content.models import Article

    article = Article.objects.select_related("website", "idea").filter(pk=article_id).first()
    if article is None:
        return "gone"
    if writer.advance(article):
        write_article.delay(article_id)
    return str(article.status)
