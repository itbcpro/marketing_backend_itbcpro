import json

import pytest

from actions.models import ActionStatus, ProposedAction
from audit.actors import Actor
from conftest import make_user
from content import services, sync
from content.models import ContentItem, PageSuggestion, WebsiteKind
from content.tests.conftest import GOOD_REVIEW, script_llm
from content.tests.fake_wordpress import BASE
from workspaces.models import Role

JSON = "application/json"
DONE = {ActionStatus.SUCCEEDED, ActionStatus.VERIFIED}


@pytest.fixture
def api(client, owner, workspace, synced, post, django_capture_on_commit_callbacks):
    client.force_login(owner)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{synced.pk}/pages/{post.pk}"

    class Api:
        def __init__(self):
            self.client, self.headers, self.base = client, headers, base

        def get(self, path=""):
            return client.get(base + path, **headers)

        def post(self, path, body=None):
            with django_capture_on_commit_callbacks(execute=True):
                return client.post(base + path, data=body or {}, content_type=JSON, **headers)

    return Api()


def test_dashboard_shows_checks_score_and_editable_fields(api):
    data = api.get().json()
    assert data["item"]["title"] == "Hello World"
    assert data["item"]["outline"] == [{"level": 2, "text": "Sub heading"}]
    assert 0 < data["score"] <= 100
    assert {c["key"] for c in data["checks"]} >= {"seo_title", "meta_description", "word_count"}
    assert data["editable"]["fields"] == ["title", "slug", "seo_title", "meta_description"]
    assert data["editable"]["seo_writable"] is True
    assert data["editable"]["seo_plugin"] == "Rank Math"
    assert data["analysis"] is None
    assert data["changes"] == []


def test_seo_fields_are_not_offered_when_the_site_does_not_expose_them(api, synced, wp):
    wp.meta_exposed = False
    services.run_probe(synced)
    data = api.get().json()
    assert data["editable"]["fields"] == ["title", "slug"]
    assert data["editable"]["seo_writable"] is False
    assert "register_post_meta" in data["editable"]["helper_snippet"]


def test_analyze_then_dashboard_lists_the_review_and_suggestions(api):
    script_llm(GOOD_REVIEW)
    data = api.post("/analyze", {"instruction": ""}).json()
    assert data["analysis"]["status"] == "done"
    assert data["analysis"]["stale"] is False
    assert data["analysis"]["result"]["primary_keyword"] == "hello world guide"
    assert {s["field"] for s in data["suggestions"]} == {"seo_title", "meta_description", "advice"}


def test_analysis_becomes_stale_when_the_page_changes(api, post):
    script_llm(GOOD_REVIEW)
    api.post("/analyze", {})
    post.content_hash = "changed"
    post.save()
    assert api.get().json()["analysis"]["stale"] is True


def test_rewrite_endpoint(api):
    script_llm({"options": ["A", "B", "C"], "note": "B"})
    data = api.post("/rewrite", {"field": "seo_title", "instruction": "punchier"}).json()
    assert data == {"options": ["A", "B", "C"], "note": "B"}


def test_apply_changes_the_site_and_records_history(api, post, wp):
    script_llm(GOOD_REVIEW)
    api.post("/analyze", {})
    response = api.post(
        "/apply",
        {
            "changes": {"seo_title": "Hello World Guide", "title": "Hello Brave World"},
            "suggestion_fields": ["seo_title"],
        },
    )
    assert response.status_code == 200, response.content
    action = ProposedAction.objects.get(pk=response.json()["action_id"])
    assert action.status in DONE, action.error
    assert action.requested_by.startswith("user:")

    row = wp.rows["post"][0]
    assert row["title"] == "Hello Brave World"
    assert row["seo_title"] == "Hello World Guide"
    assert wp.posted[-1]["meta"] == {"rank_math_title": "Hello World Guide"}

    post.refresh_from_db()
    assert post.title == "Hello Brave World"
    assert post.seo["title"] == "Hello World Guide"
    assert not PageSuggestion.objects.filter(item=post, field="seo_title", status="proposed")

    history = api.get().json()["changes"]
    assert len(history) == 1
    assert history[0]["can_rollback"] is True
    assert history[0]["before"]["title"] == "Hello World"


def test_rollback_restores_the_previous_values(api, wp):
    api.post("/apply", {"changes": {"title": "Changed", "meta_description": "A new description"}})
    assert wp.rows["post"][0]["title"] == "Changed"
    change = api.get().json()["changes"][0]
    result = api.post(f"/changes/{change['id']}/rollback")
    assert result.status_code == 200, result.content
    row = wp.rows["post"][0]
    assert row["title"] == "Hello World"
    assert row["description"] == "A description"
    changes = api.get().json()["changes"]
    assert changes[0]["is_rollback"] is True
    assert all(not c["can_rollback"] for c in changes)


def test_edit_made_on_the_site_meanwhile_blocks_the_change(api, wp):
    wp.edit("post", 1, title="Edited by a colleague")
    wp.rows["post"][0]["modified"] = "2999-01-01T00:00:00"
    response = api.post("/apply", {"changes": {"title": "My version"}})
    action = ProposedAction.objects.get(pk=response.json()["action_id"])
    assert action.status not in DONE
    assert "page_changed_on_site" in action.error
    assert wp.rows["post"][0]["title"] == "Edited by a colleague"
    assert wp.posted == []


def test_unchanged_or_unknown_fields_are_refused(api, post):
    assert api.post("/apply", {"changes": {"title": post.title}}).status_code == 422
    assert api.post("/apply", {"changes": {"content": "<p>x</p>"}}).status_code == 422


def test_seo_changes_are_refused_when_not_writable(api, synced, wp):
    wp.meta_exposed = False
    services.run_probe(synced)
    assert api.post("/apply", {"changes": {"seo_title": "New"}}).status_code == 422


def test_dismiss_a_suggestion(api):
    script_llm(GOOD_REVIEW)
    data = api.post("/analyze", {}).json()
    sid = data["suggestions"][0]["id"]
    after = api.post(f"/suggestions/{sid}/dismiss").json()
    assert sid not in [s["id"] for s in after["suggestions"]]


def test_viewers_can_read_but_not_analyze_or_apply(client, workspace, synced, post):
    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    client.force_login(viewer)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{synced.pk}/pages/{post.pk}"
    assert client.get(base, **headers).status_code == 200
    assert client.post(base + "/analyze", data={}, content_type=JSON, **headers).status_code == 403
    body = json.dumps({"changes": {"title": "x"}})
    assert client.post(base + "/apply", data=body, content_type=JSON, **headers).status_code == 403


def test_git_site_pages_get_a_dashboard_but_no_editing(client, owner, workspace, wp, monkeypatch):
    monkeypatch.setattr(sync, "sleeper", lambda _seconds: None)
    wp.public_pages["/about/"] = (
        "<html><head><title>About</title></head><body><h1>About</h1></body></html>"
    )
    site = services.create_website(
        workspace_id=workspace.pk,
        name="AI site",
        kind=WebsiteKind.GIT_SITE,
        base_url=BASE,
        actor=Actor.system("test"),
    )
    sync.execute_run(sync.start_run(site, full=True))
    item = ContentItem.objects.get(website=site, url=f"{BASE}/about/")
    client.force_login(owner)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{site.pk}/pages/{item.pk}"
    data = client.get(base, **headers).json()
    assert data["editable"]["fields"] == []
    assert data["editable"]["reason"] == "git_site"
    assert any(c["key"] == "h1" and c["status"] == "pass" for c in data["checks"])
    apply = client.post(
        base + "/apply", data={"changes": {"title": "New"}}, content_type=JSON, **headers
    )
    assert apply.status_code == 422


def test_brief_is_saved_merged_and_sent_to_the_reviewer(api, synced, post, client, workspace):
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    site = client.put(
        f"/api/v1/websites/{synced.pk}/brief",
        data={"country": "Canada", "region": "Vancouver", "audience": "Small businesses"},
        content_type=JSON,
        **headers,
    )
    assert site.status_code == 200, site.content
    saved = api.client.put(
        f"/api/v1/websites/{synced.pk}/pages/{post.pk}/brief",
        data={
            "primary_keyword": "managed it services",
            "secondary_keywords": ["it support", " "],
            "goal": "leads",
        },
        content_type=JSON,
        **headers,
    )
    assert saved.status_code == 200, saved.content
    body = saved.json()
    assert body["brief"]["secondary_keywords"] == ["it support"]
    assert body["site_brief"]["region"] == "Vancouver"

    gateway = script_llm(GOOD_REVIEW)
    api.post("/analyze", {})
    system = gateway.requests[0][0].system[0].text
    for text in ("managed it services", "Vancouver", "Small businesses", "leads"):
        assert text in system


def test_invalid_goal_is_refused(api):
    response = api.client.put(
        api.base + "/brief", data={"goal": "world domination"}, content_type=JSON, **api.headers
    )
    assert response.status_code in {422, 400}


def test_advice_endpoint(api):
    script_llm(
        {"why_it_matters": "Why.", "steps": ["Do it"], "example": "Example."},
    )
    ok = api.post("/advice", {"check": "meta_description"})
    assert ok.status_code == 200, ok.content
    assert ok.json()["steps"] == ["Do it"]
    assert api.post("/advice", {"check": "nope"}).status_code == 422


def test_dashboard_shows_the_written_seo_text_even_if_the_plugin_cache_is_stale(api, post, wp):
    wp.stale_seo = True
    response = api.post(
        "/apply",
        {
            "changes": {
                "meta_description": "Fresh description written by the owner for this page. It says what the visitor gets and why to read on."
            }
        },
    )
    action = ProposedAction.objects.get(pk=response.json()["action_id"])
    assert action.status in DONE, action.error
    data = api.get().json()
    assert data["item"]["seo"]["description"].startswith("Fresh description")
    checks = {c["key"]: c["status"] for c in data["checks"]}
    assert checks["meta_description"] == "pass"


def test_an_invalid_slug_is_a_validation_error_not_a_crash(api):
    assert api.post("/apply", {"changes": {"slug": "Not A Slug"}}).status_code == 422
