from datetime import date, timedelta
from itertools import pairwise

import pytest

from content import gsc_sync, search
from content.google import gsc
from content.models import (
    GscDaily,
    GscPageDaily,
    GscQueryDaily,
    GscSitemap,
    SearchProperty,
    SyncStatus,
)
from content.tests.conftest import TODAY
from content.tests.fake_google import SITE

END = TODAY - timedelta(days=gsc_sync.DATA_LAG_DAYS)


def run(site, *, full=False):
    return gsc_sync.execute_run(gsc_sync.start_run(site, full=full))


def test_windows():
    prop = SearchProperty(last_date=None)
    start, end = gsc_sync.sync_window(prop, full=False, now=TODAY)
    assert end == END
    assert (end - start).days + 1 == gsc_sync.BACKFILL_DAYS
    prop.last_date = END
    start, end = gsc_sync.sync_window(prop, full=False, now=TODAY)
    assert (end - start).days + 1 == gsc_sync.OVERLAP_DAYS
    prop.last_date = END - timedelta(days=40)
    start, _ = gsc_sync.sync_window(prop, full=False, now=TODAY)
    assert start == END - timedelta(days=40) - timedelta(days=gsc_sync.OVERLAP_DAYS - 1)


def test_chunks_cover_the_range_without_gaps():
    first, last = date(2026, 1, 1), date(2026, 3, 15)
    parts = gsc_sync.chunks(first, last, 30)
    assert parts[0][0] == first
    assert parts[-1][1] == last
    for (_, stop), (nxt, _) in pairwise(parts):
        assert nxt == stop + timedelta(days=1)


def test_first_sync_backfills_sixteen_months(linked, google):
    result = run(linked)
    assert result.status == SyncStatus.DONE, result.error
    days = gsc_sync.BACKFILL_DAYS
    assert result.stats["days"] == days
    assert result.stats["pages"] == days * 2
    assert result.stats["queries"] == days * 3
    assert GscDaily.objects.filter(website=linked).count() == days
    prop = SearchProperty.objects.get(website=linked)
    assert prop.last_date == END
    assert prop.first_date == END - timedelta(days=days - 1)
    assert prop.last_synced_at is not None
    day = GscDaily.objects.get(website=linked, date=END)
    assert (day.clicks, day.impressions) == (11, 100)
    assert round(day.position, 1) == 5.2
    # all queries are asked for web results only, never more than a chunk at a time
    for body in google.queries:
        span = date.fromisoformat(body["endDate"]) - date.fromisoformat(body["startDate"])
        assert span.days < gsc_sync.CHUNK_DAYS
        assert body["type"] == "web"
        assert body["dataState"] == "final"


def test_second_sync_only_reads_the_recent_days_and_overwrites_restated_numbers(linked, google):
    run(linked)
    google.queries.clear()
    google.overrides[("managed it services", END.isoformat())] = {"clicks": 99}
    result = run(linked)
    assert result.status == SyncStatus.DONE, result.error
    assert result.stats["days"] == gsc_sync.OVERLAP_DAYS
    assert len(google.queries) == 3
    restated = GscQueryDaily.objects.get(
        website=linked,
        date=END,
        query="managed it services",
        page="https://wp.example.com/hello-world/",
    )
    assert restated.clicks == 99
    assert GscQueryDaily.objects.filter(website=linked).count() == gsc_sync.BACKFILL_DAYS * 3


def test_a_full_run_reads_everything_again(linked, google):
    run(linked)
    google.queries.clear()
    result = run(linked, full=True)
    assert result.stats["days"] == gsc_sync.BACKFILL_DAYS
    assert len(google.queries) > 3


def test_rows_are_read_page_by_page(linked, google, monkeypatch):
    monkeypatch.setattr(gsc, "ROW_LIMIT", 4)
    monkeypatch.setattr(gsc_sync, "BACKFILL_DAYS", 5)
    result = run(linked)
    assert result.status == SyncStatus.DONE, result.error
    assert GscQueryDaily.objects.filter(website=linked).count() == 15
    assert any(q["startRow"] > 0 for q in google.queries)


def test_sitemaps_are_copied_and_removed_when_gone(linked, google):
    run(linked)
    sitemap = GscSitemap.objects.get(website=linked)
    assert sitemap.is_index
    assert sitemap.warnings == 1
    assert sitemap.contents[0]["submitted"] == "105"
    google.sitemaps = []
    run(linked)
    assert GscSitemap.objects.filter(website=linked).count() == 0


def test_an_expired_authorisation_fails_the_run_and_is_recorded(linked, google):
    google.token = "changed-on-google"
    result = run(linked)
    assert result.status == SyncStatus.FAILED
    assert result.error == "google_unauthorised"
    assert SearchProperty.objects.get(website=linked).last_error == "google_unauthorised"
    assert GscDaily.objects.filter(website=linked).count() == 0


def test_a_google_error_fails_the_run(linked, google):
    google.status_for_queries = 403
    result = run(linked)
    assert result.status == SyncStatus.FAILED
    assert result.error


def test_sync_without_a_property_fails_cleanly(wp_site):
    result = run(wp_site)
    assert result.status == SyncStatus.FAILED
    assert result.error == "no_search_property"


def test_linking_resets_the_copied_range(linked, google_connection):
    run(linked)
    search.link(linked, google_connection, SITE)
    prop = SearchProperty.objects.get(website=linked)
    assert prop.last_date is None
    assert gsc_sync.sync_window(prop, full=False, now=TODAY)[0] == END - timedelta(
        days=gsc_sync.BACKFILL_DAYS - 1
    )


@pytest.mark.parametrize("model", [GscDaily, GscPageDaily, GscQueryDaily])
def test_unlinking_removes_the_copied_data(linked, model):
    run(linked)
    assert model.objects.filter(website=linked).exists()
    search.unlink(linked)
    assert not model.objects.filter(website=linked).exists()
    assert not SearchProperty.objects.filter(website=linked).exists()
