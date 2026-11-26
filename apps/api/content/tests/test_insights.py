from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from content import search_report, speed
from content.google import pagespeed
from content.models import (
    ContentItem,
    GscDaily,
    GscPageDaily,
    GscQueryDaily,
    PageSpeedResult,
    SearchProperty,
)
from content.tests.conftest import TODAY, script_llm

JSON = "application/json"
LAST = TODAY - timedelta(days=2)
BASE = "https://wp.example.com"
A, B, C, D = (f"{BASE}/{n}/" for n in "abcd")
PREV_DAY = LAST - timedelta(days=30)  # inside the 28 days before the current 28-day window


@pytest.fixture
def api(client, owner, workspace, wp_site, django_capture_on_commit_callbacks):
    client.force_login(owner)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{wp_site.pk}"

    class Api:
        site = wp_site

        def get(self, path, **params):
            return client.get(base + path, params, **headers)

        def post(self, path, body=None):
            with django_capture_on_commit_callbacks(execute=True):
                return client.post(base + path, data=body or {}, content_type=JSON, **headers)

    return Api()


def _add(site, day, page, query, clicks, impressions, position):
    kw = {"workspace_id": site.workspace_id, "website": site, "date": day}
    stats = {"clicks": clicks, "impressions": impressions, "position": position}
    GscPageDaily.objects.create(page=page, **kw, **stats)
    GscQueryDaily.objects.create(page=page, query=query, **kw, **stats)


@pytest.fixture
def data(wp_site):
    now, before = LAST - timedelta(days=3), PREV_DAY
    SearchProperty.objects.create(
        workspace_id=wp_site.workspace_id,
        website=wp_site,
        site_url="sc-domain:wp.example.com",
        first_date=before,
        last_date=LAST,
    )
    # A falls, B rises, C disappears, D is new, E has many impressions and few clicks.
    rows = [
        (before, A, "managed it", 30, 300, 3.0),
        (now, A, "managed it", 10, 300, 3.5),
        (before, B, "it support", 5, 200, 9.0),
        (now, B, "it support", 20, 400, 6.0),
        (before, C, "voip phones", 6, 100, 12.0),
        (now, D, "cloud backup", 4, 80, 15.0),
        (now, f"{BASE}/e/", "msp vancouver", 2, 900, 4.0),
    ]
    for day, page, query, clicks, impressions, position in rows:
        _add(wp_site, day, page, query, clicks, impressions, position)
    for day, clicks, impressions in ((before, 41, 600), (now, 36, 1780)):
        GscDaily.objects.create(
            workspace_id=wp_site.workspace_id,
            website=wp_site,
            date=day,
            clicks=clicks,
            impressions=impressions,
            position=5.0,
        )
    return wp_site


def test_the_comparison_period_is_the_one_before_or_a_year_earlier(data):
    win = search_report.resolve_window(data, days=28)
    assert (win.end - win.start).days == 27
    assert win.prev_end == win.start - timedelta(days=1)
    year = search_report.resolve_window(data, days=28, compare="year")
    assert (win.start - year.prev_start).days == 364
    assert search_report.resolve_window(data, days=28, compare="none").prev_start is None


def test_a_custom_period_is_capped_to_the_last_day_with_data(data):
    win = search_report.resolve_window(
        data, start=LAST - timedelta(days=9), end=LAST + timedelta(days=5)
    )
    assert win.end == LAST and win.days == 10


def test_report_compares_totals_and_filters_by_query_or_page(data):
    win = search_report.resolve_window(data, days=28)
    whole = search_report.report(data, win)
    assert whole["totals"]["clicks"] == 36 and whole["previous"]["clicks"] == 41
    assert whole["delta"]["clicks"] == -5
    narrow = search_report.report(data, win, text="support")
    assert narrow["totals"]["clicks"] == 20 and narrow["previous"]["clicks"] == 5
    only_page = search_report.report(data, win, page="/a/")
    assert only_page["totals"]["clicks"] == 10 and only_page["previous"]["clicks"] == 30
    assert search_report.report(data, None) == {"has_data": False}


def test_rows_show_change_status_and_sort(data):
    win = search_report.resolve_window(data, days=28)
    pages = {r["page"]: r for r in search_report.rows(data, win, kind="page")}
    assert pages[A]["status"] == "down" and pages[A]["d_clicks"] == -20
    assert pages[B]["status"] == "up" and pages[B]["d_position"] == -3.0
    assert pages[C]["status"] == "lost" and pages[D]["status"] == "new"
    worst_first = search_report.rows(data, win, kind="page", sort="clicks_change")
    assert worst_first[0]["page"] == A
    assert [r["page"] for r in search_report.rows(data, win, kind="page", status="lost")] == [C]
    queries = search_report.rows(data, win, kind="query", text="it ", page=B)
    assert [r["query"] for r in queries] == ["it support"]
    by_query = search_report.rows(data, win, kind="page", text="managed")
    assert [r["page"] for r in by_query] == [A]
    assert (
        search_report.rows(data, win, kind="page", min_impressions=500)[0]["page"] == f"{BASE}/e/"
    )


def test_opportunities_find_low_ctr_striking_distance_and_losses(data):
    found = search_report.opportunities(data, search_report.resolve_window(data, days=28))
    assert [r["query"] for r in found["low_ctr_queries"]] == ["msp vancouver", "managed it"]
    assert {r["query"] for r in found["striking_distance_queries"]} == {"cloud backup"}
    assert [r["page"] for r in found["declining_pages"]] == [A]
    assert [r["page"] for r in found["lost_pages"]] == [C]


def test_report_endpoints_accept_dates_filters_and_comparison(api, data):
    body = api.get("/search/report", days=28, q="support").json()
    assert body["totals"]["clicks"] == 20 and body["delta"]["clicks"] == 15
    none = api.get("/search/report", days=28, compare="none").json()
    assert none["previous"] is None
    rows = api.get("/search/rows", kind="page", sort="clicks_change", limit=2).json()
    assert rows[0]["page"] == A and len(rows) == 2
    assert api.get("/search/rows", kind="nope").status_code == 422
    assert api.get("/search/report", compare="sideways").status_code == 422
    assert api.get("/search/opportunities").json()["has_data"] is True


def _analysis(urls):
    return {
        "summary": "Clicks fell a little while impressions grew.",
        "verdict": "mixed",
        "what_changed": ["Clicks fell from 41 to 36"],
        "progress_since_last": "No earlier analysis.",
        "problems": [
            {
                "severity": "high",
                "area": "titles and descriptions",
                "evidence": "msp vancouver: 900 impressions, 2 clicks",
                "fix": "Rewrite the title",
            }
        ],
        "pages_to_update": [
            {
                "url": url,
                "priority": "high",
                "why": "Lost clicks",
                "what_to_change": ["Rewrite the title"],
                "target_queries": ["managed it"],
            }
            for url in urls
        ],
        "quick_wins": [
            {
                "action": "Rewrite title",
                "url": "https://elsewhere.com/x",
                "expected_effect": "more clicks",
            }
        ],
        "new_content": [
            {"topic": "Cloud backup guide", "query": "cloud backup", "why": "80 impressions"}
        ],
        "cannibalization": [],
        "speed_notes": [],
        "next_steps": ["Fix the title of the first page"],
    }


def test_ai_analysis_is_saved_and_drops_invented_addresses(api, data):
    gateway = script_llm(_analysis([A, "https://invented.example/page/"]))
    started = api.post("/search/analyses", {"days": 28, "instruction": "focus on titles"})
    assert started.status_code == 200, started.content
    stored = api.get("/search/analyses").json()[0]
    assert stored["status"] == "done", stored
    result = stored["result"]
    assert [p["url"] for p in result["pages_to_update"]] == [A]
    assert result["quick_wins"] == []  # its address is not one of the site's pages
    system = gateway.requests[-1][0].system[0].text
    assert "focus on titles" in system and "managed it" in system
    assert api.get(f"/search/analyses/{stored['id']}").json()["status"] == "done"


def test_a_second_analysis_sees_the_first_one(api, data):
    script_llm(_analysis([A]), _analysis([A]))
    api.post("/search/analyses", {"days": 28})
    gateway = script_llm(_analysis([A]))
    api.post("/search/analyses", {"days": 28})
    assert "pages_it_recommended_changing" in gateway.requests[-1][0].system[0].text


def test_analysis_needs_data(api):
    assert api.post("/search/analyses", {"days": 28}).status_code == 422


PSI = {
    "lighthouseResult": {
        "categories": {
            "performance": {"score": 0.62},
            "seo": {"score": 0.91},
            "accessibility": {"score": 0.88},
            "best-practices": {"score": 1.0},
        },
        "audits": {
            "largest-contentful-paint": {"numericValue": 4200.4},
            "cumulative-layout-shift": {"numericValue": 0.12},
            "total-blocking-time": {"numericValue": 310},
            "first-contentful-paint": {"numericValue": 1800},
            "speed-index": {"numericValue": 3900},
            "uses-optimized-images": {
                "title": "Efficiently encode images",
                "score": 0.3,
                "scoreDisplayMode": "numeric",
                "displayValue": "Potential savings of 480 KiB",
                "details": {"overallSavingsMs": 900},
            },
            "viewport": {"title": "Has viewport", "score": 1, "scoreDisplayMode": "binary"},
        },
    },
    "loadingExperience": {
        "overall_category": "AVERAGE",
        "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 3100, "category": "AVERAGE"},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 180, "category": "FAST"},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 12, "category": "AVERAGE"},
        },
    },
}


@pytest.fixture
def psi(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        if calls_fail:
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, json=PSI)

    calls_fail = []
    monkeypatch.setattr(pagespeed, "transport", httpx.MockTransport(handler))

    class Psi:
        urls = calls
        fail = calls_fail

    return Psi()


def test_pagespeed_numbers_are_parsed():
    out = pagespeed.parse(PSI)
    assert (out["performance"], out["seo"], out["accessibility"], out["best_practices"]) == (
        62,
        91,
        88,
        100,
    )
    assert out["lab"]["lcp_ms"] == 4200.4 and out["lab"]["cls"] == 0.12
    assert out["field"]["lcp_ms"] == 3100 and out["field"]["cls"] == 0.12
    assert [o["id"] for o in out["opportunities"]] == ["uses-optimized-images"]


def test_speed_runs_are_queued_run_and_compared(api, data, psi, settings):
    settings.GOOGLE_PAGESPEED_API_KEY = "k"
    first = api.post("/speed", {"urls": [A], "strategies": ["mobile", "desktop"]})
    assert first.status_code == 200, first.content
    assert {r["strategy"] for r in first.json()} == {"mobile", "desktop"}
    assert len(psi.urls) == 2 and "key=k" in str(psi.urls[0]) and "category=SEO" in str(psi.urls[0])
    latest = api.get("/speed").json()
    assert {r["status"] for r in latest} == {"done"}
    mobile = next(r for r in latest if r["strategy"] == "mobile")
    assert mobile["performance"] == 62 and mobile["previous"] is None
    PageSpeedResult.objects.update(created_at=timezone.now() - timedelta(days=3))
    api.post("/speed", {"urls": [A], "strategies": ["mobile"]})
    again = next(r for r in api.get("/speed").json() if r["strategy"] == "mobile")
    assert again["previous"]["performance"] == 62
    history = api.get("/speed/history", url=A, strategy="mobile").json()
    assert len(history) == 2


def test_speed_defaults_to_the_top_search_pages_and_rejects_other_sites(api, data, psi):
    started = api.post("/speed", {"top": 2, "strategies": ["mobile"]})
    assert [r["url"] for r in started.json()] == [B, A]
    bad = api.post("/speed", {"urls": ["https://other.org/"], "strategies": ["mobile"]})
    assert bad.status_code == 422
    assert api.post("/speed", {"urls": [A], "strategies": ["tv"]}).status_code == 422


def test_a_quota_error_is_kept_as_a_failed_run(api, data, psi):
    psi.fail.append(1)
    api.post("/speed", {"urls": [A], "strategies": ["mobile"]})
    row = api.get("/speed").json()[0]
    assert row["status"] == "failed" and "quota" in row["error"]


def test_the_ai_sees_speed_results(api, data, psi):
    api.post("/speed", {"urls": [A], "strategies": ["mobile"]})
    ContentItem.objects.count()
    gateway = script_llm(_analysis([A]))
    api.post("/search/analyses", {"days": 28})
    assert "Efficiently encode images" in gateway.requests[-1][0].system[0].text


def test_speed_prompt_view_is_empty_without_runs(data):
    assert speed.prompt_view(data) == []
