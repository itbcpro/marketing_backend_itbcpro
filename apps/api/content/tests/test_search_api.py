from datetime import timedelta

import pytest

from conftest import make_user
from content import gsc_sync, review, search
from content.models import ContentItem, SearchProperty
from content.tests.conftest import GOOD_REVIEW, TODAY, script_llm
from content.tests.fake_google import SITE
from core.errors import ValidationFailedError
from workspaces.models import Role

JSON = "application/json"
END = TODAY - timedelta(days=gsc_sync.DATA_LAG_DAYS)


@pytest.fixture
def api(client, owner, workspace, wp_site, django_capture_on_commit_callbacks):
    client.force_login(owner)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{wp_site.pk}"

    class Api:
        site = wp_site
        http = client

        def get(self, path, **params):
            return client.get(base + path, params, **headers)

        def post(self, path, body=None):
            with django_capture_on_commit_callbacks(execute=True):
                return client.post(base + path, data=body or {}, content_type=JSON, **headers)

    return Api()


def sites(*pairs):
    return [{"site_url": url, "permission": level} for url, level in pairs]


def test_best_match_prefers_the_domain_property(wp_site):
    found = sites(
        ("https://wp.example.com/", "siteOwner"), ("sc-domain:wp.example.com", "siteFullUser")
    )
    assert search.best_match(wp_site, found) == "sc-domain:wp.example.com"


def test_best_match_falls_back_to_the_url_prefix_and_skips_unverified(wp_site):
    assert (
        search.best_match(wp_site, sites(("https://wp.example.com/", "siteOwner")))
        == "https://wp.example.com/"
    )
    unverified = sites(("sc-domain:wp.example.com", "siteUnverifiedUser"))
    assert search.best_match(wp_site, unverified) is None
    assert search.best_match(wp_site, sites(("sc-domain:other.org", "siteOwner"))) is None


def test_link_refuses_a_property_the_account_cannot_read(wp_site, google_connection):
    with pytest.raises(ValidationFailedError):
        search.link(wp_site, google_connection, "sc-domain:not-mine.org")


def test_status_before_and_after_connecting(api, google_keys):
    data = api.get("/search").json()
    assert data["keys_missing"] == []
    assert data["connections"] == []
    assert data["property"] is None
    assert data["redirect_uri"].endswith("/connections/oauth/google_search/callback")


def test_status_reports_missing_keys(api, settings):
    settings.GOOGLE_OAUTH_CLIENT_ID = ""
    settings.GOOGLE_OAUTH_CLIENT_SECRET = ""
    assert api.get("/search").json()["keys_missing"] == ["client_id", "client_secret"]


def test_properties_lists_sites_with_a_suggestion(api, google_connection):
    data = api.get(f"/search/properties/{google_connection.pk}").json()
    assert data["suggested"] == SITE
    assert data["sites"][0]["permission"] == "siteOwner"


def test_link_starts_a_backfill_and_data_is_readable(api, google_connection):
    response = api.post(
        "/search/link", {"connection_id": str(google_connection.pk), "site_url": SITE}
    )
    assert response.status_code == 200, response.content
    status = response.json()
    assert status["property"]["site_url"] == SITE
    assert status["last_run"]["status"] == "done"
    assert status["property"]["last_date"] == END.isoformat()

    overview = api.get("/search/overview", days=28).json()
    assert overview["has_data"] is True
    assert overview["totals"] == {"clicks": 308, "impressions": 2800, "ctr": 0.11, "position": 5.2}
    assert overview["delta"]["clicks"] == 0
    assert len(overview["series"]) == 28
    assert overview["queries"][0]["query"] == "managed it services"
    assert overview["queries"][0]["position"] == 3.0
    assert overview["pages"][0]["page"] == "https://wp.example.com/hello-world/"


def test_queries_pages_and_sitemaps(api, linked):
    run = gsc_sync.execute_run(gsc_sync.start_run(linked, full=True))
    assert run.status == "done", run.error
    found = api.get("/search/queries", days=28, q="support").json()
    assert [row["query"] for row in found] == ["it support"]
    assert found[0]["clicks"] == 56
    pages = api.get("/search/pages", days=7).json()
    assert [p["page"] for p in pages] == [
        "https://wp.example.com/hello-world/",
        "https://wp.example.com/about/",
    ]
    assert api.get("/search/sitemaps").json()[0]["contents"][0]["submitted"] == "105"


def test_overview_without_data(api):
    assert api.get("/search/overview").json() == {"has_data": False}
    assert api.get("/search/queries").json() == []


def test_sync_endpoint_needs_a_property(api):
    assert api.post("/search/sync", {}).status_code == 400


def test_sync_endpoint_runs_a_sync(api, linked):
    response = api.post("/search/sync", {"full": False})
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "done"


def test_unlink(api, linked):
    gsc_sync.execute_run(gsc_sync.start_run(linked, full=True))
    assert (
        api.http.delete(f"/api/v1/websites/{linked.pk}/search/link", **api_headers(api)).status_code
        == 204
    )
    assert not SearchProperty.objects.filter(website=linked).exists()


def api_headers(api):
    return {"HTTP_X_WORKSPACE_ID": str(api.site.workspace_id)}


def test_page_search_matches_every_spelling_of_the_address(api, linked):
    gsc_sync.execute_run(gsc_sync.start_run(linked, full=True))
    item = ContentItem.objects.create(
        workspace_id=linked.workspace_id,
        website=linked,
        key="wp:post:1",
        source="wordpress",
        kind="post",
        url="https://wp.example.com/hello-world",
    )
    data = search.page_search(linked, item.url, 28)
    assert data["totals"]["clicks"] == 196
    assert data["queries"][0]["query"] == "managed it services"
    assert data["totals"]["position"] == 4.0
    assert len(data["series"]) == 28


def test_page_search_endpoint(api, linked):
    gsc_sync.execute_run(gsc_sync.start_run(linked, full=True))
    item = ContentItem.objects.create(
        workspace_id=linked.workspace_id,
        website=linked,
        key="wp:page:1",
        source="wordpress",
        kind="page",
        url="https://wp.example.com/about/",
    )
    data = api.get(f"/pages/{item.pk}/search", days=28).json()
    assert data["totals"]["clicks"] == 112
    assert data["queries"][0]["query"] == "about acme"


def test_the_reviewer_is_told_the_measured_search_numbers(linked, google):
    gsc_sync.execute_run(gsc_sync.start_run(linked, full=True))
    item = (
        ContentItem.objects.get(website=linked, key="wp:post:1")
        if ContentItem.objects.filter(website=linked, key="wp:post:1").exists()
        else None
    )
    if item is None:
        from content import sync

        sync.execute_run(sync.start_run(linked, full=True))
        item = ContentItem.objects.get(website=linked, key="wp:post:1")
    gateway = script_llm(GOOD_REVIEW)
    analysis = review.start_analysis(item, requested_by="user:1")
    review.run_analysis(analysis)
    system = gateway.requests[0][0].system[0].text
    assert "managed it services" in system
    assert '"available": true' in system


def test_the_reviewer_is_told_when_there_are_no_search_numbers(post):
    gateway = script_llm(GOOD_REVIEW)
    review.run_analysis(review.start_analysis(post, requested_by="user:1"))
    system = gateway.requests[0][0].system[0].text
    assert "no search numbers" in system


def test_viewers_can_read_search_data_but_not_connect(client, workspace, linked):
    viewer = make_user("viewer2@example.com", workspace, Role.VIEWER)
    client.force_login(viewer)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{linked.pk}"
    assert client.get(base + "/search", **headers).status_code == 200
    assert (
        client.post(base + "/search/sync", data={}, content_type=JSON, **headers).status_code == 403
    )


def test_a_run_that_waits_too_long_is_reported_as_stale(api, linked):
    from datetime import timedelta

    from django.utils import timezone

    from content.models import SyncRun

    run = gsc_sync.start_run(linked)
    assert api.get("/search").json()["last_run"]["stale"] is False
    SyncRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(minutes=5))
    status = api.get("/search").json()["last_run"]
    assert status["status"] == "queued"
    assert status["stale"] is True
