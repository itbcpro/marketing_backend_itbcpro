import pytest
from django.test import Client

from actions.models import ActionStatus, ProposedAction
from conftest import make_user
from content import alt
from content.models import ContentItem
from content.normalize import normalize_html
from content.tests.conftest import script_llm
from content.tests.fake_wordpress import BASE
from workspaces.models import Role

JSON = "application/json"
DONE = {ActionStatus.SUCCEEDED, ActionStatus.VERIFIED}

HTML = """
<h2>Our partners</h2><p>Trusted by these organisations every year.</p>
<img class="size-full wp-image-77" src="/uploads/acme-logo.png" alt="" width="200" height="80">
<img src="data:image/gif;base64,AAAA" data-lazy-src="https://cdn.example.com/lazy.jpg" alt="  ">
<img src="/uploads/acme-logo.png" alt="">
<img src="/uploads/described.png" alt="A described picture">
"""


def test_images_are_read_with_their_context():
    facts = normalize_html(HTML, "https://example.com")
    images = facts["images"]
    assert [i["src"] for i in images] == [
        "https://example.com/uploads/acme-logo.png",
        "https://cdn.example.com/lazy.jpg",
        "https://example.com/uploads/described.png",
    ]
    first = images[0]
    assert first["attachment_id"] == 77
    assert first["alt"] == ""
    assert (first["width"], first["height"]) == ("200", "80")
    assert first["heading"] == "Our partners"
    assert "Trusted by these organisations" in first["near"]
    assert images[2]["alt"] == "A described picture"
    assert facts["stats"]["images"] == 4
    assert facts["stats"]["images_missing_alt"] == 3


def test_a_site_sync_stores_the_images(synced):
    page = ContentItem.objects.get(website=synced, key="wp:page:11")
    by_file = {alt.file_name(i["src"]): i for i in page.images}
    assert by_file["team.jpg"]["attachment_id"] == 201
    assert by_file["icon.svg"]["attachment_id"] == 202
    assert by_file["hotlink.png"]["attachment_id"] is None
    assert page.images[0]["heading"] == "Backup"
    post = ContentItem.objects.get(website=synced, key="wp:post:1")
    assert [i["alt"] for i in post.images] == ["", "ok"]


def reply(*items):
    return {"items": [{"index": n, **row} for n, row in enumerate(items, start=1)]}


@pytest.fixture
def page(synced):
    return ContentItem.objects.get(website=synced, key="wp:page:11")


def test_suggestions_look_at_the_pictures_and_map_back_to_the_images(page):
    gateway = script_llm(
        reply(
            {
                "alt": "Team members planning a backup schedule",
                "decorative": False,
                "note": "Photo",
            },
            {"alt": "should be dropped", "decorative": True, "note": "Ornament"},
            {"alt": "Partner logo", "decorative": False, "note": "From the file name"},
        )
    )
    found = alt.suggest(page)
    assert [f["attachment_id"] for f in found] == [201, 202, None]
    assert found[0]["suggested"] == "Team members planning a backup schedule"
    assert found[0]["seen"] is True
    assert found[1]["decorative"] is True
    assert found[1]["suggested"] == ""
    assert found[1]["seen"] is False  # SVG cannot be shown to the model
    assert found[2]["seen"] is False  # the hotlinked file could not be read
    blocks = gateway.requests[0][0].messages[0].content
    pictures = [b for b in blocks if getattr(b, "raw", {}).get("type") == "image"]
    assert len(pictures) == 1
    assert pictures[0].raw["source"]["media_type"] == "image/jpeg"
    system = gateway.requests[0][0].system[0].text
    assert "team.jpg" in system
    assert "untrusted" in system.lower()


def test_only_images_without_alt_text_are_suggested(synced):
    post = ContentItem.objects.get(website=synced, key="wp:post:1")
    script_llm(reply({"alt": "A logo", "decorative": False, "note": ""}))
    found = alt.suggest(post)
    assert len(found) == 1
    assert found[0]["attachment_id"] == 101


def test_long_suggestions_are_cut_to_the_limit(page):
    script_llm(reply({"alt": "word " * 100, "decorative": False, "note": ""}))
    found = alt.suggest(page, [page.images[0]["src"]])
    assert len(found) == 1
    assert len(found[0]["suggested"]) <= alt.MAX_ALT


@pytest.fixture
def api(client: Client, owner, workspace, synced, page, django_capture_on_commit_callbacks):
    client.force_login(owner)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{synced.pk}/pages/{page.pk}"

    class Api:
        def __init__(self):
            self.http, self.headers, self.base = client, headers, base

        def get(self, path=""):
            return client.get(base + path, **headers)

        def post(self, path, body=None):
            with django_capture_on_commit_callbacks(execute=True):
                return client.post(base + path, data=body or {}, content_type=JSON, **headers)

    return Api()


def test_dashboard_lists_the_images(api):
    images = api.get().json()["item"]["images"]
    assert {i["attachment_id"] for i in images} == {201, 202, None}


def test_suggest_endpoint(api, page):
    script_llm(reply({"alt": "Team photo", "decorative": False, "note": "x"}))
    response = api.post("/alt/suggest", {"srcs": [page.images[0]["src"]]})
    assert response.status_code == 200, response.content
    assert response.json()[0]["suggested"] == "Team photo"


def test_apply_changes_the_media_library_and_updates_the_page(api, page, wp):
    response = api.post(
        "/alt/apply",
        {"alts": {"201": "Team planning a backup schedule", "202": "Backup icon"}},
    )
    assert response.status_code == 200, response.content
    action = ProposedAction.objects.get(pk=response.json()["action_id"])
    assert action.status in DONE, action.error
    assert wp.media[201]["alt"] == "Team planning a backup schedule"
    assert wp.media[202]["alt"] == "Backup icon"
    page.refresh_from_db()
    alts = {alt.file_name(i["src"]): i["alt"] for i in page.images}
    assert alts["team.jpg"] == "Team planning a backup schedule"
    assert page.stats["images_missing_alt"] == 1  # only the hotlinked picture is left
    history = api.get().json()["changes"]
    assert history[0]["can_rollback"] is True
    assert history[0]["before"] == {"201": "", "202": ""}


HARD_CODED = (
    '<h2>Backup</h2><img class="wp-image-201 size-full" src="{base}/wp-content/uploads/team.jpg"'
    ' alt="">'
    '<img class="wp-image-202" src="{base}/wp-content/uploads/icon.svg" alt="Written by hand">'
)


def test_apply_writes_the_alt_text_into_the_stored_body_too(api, page, wp):
    wp.edit("page", 11, content=HARD_CODED.format(base=BASE))  # block editor: alt lives in the body
    response = api.post(
        "/alt/apply", {"alts": {"201": 'Team "A" planning a backup', "202": "Backup icon"}}
    )
    assert response.status_code == 200, response.content
    action = ProposedAction.objects.get(pk=response.json()["action_id"])
    assert action.status in DONE, action.error
    stored = next(r for r in wp.rows["page"] if r["id"] == 11)["content"]
    assert 'alt="Team &quot;A&quot; planning a backup"' in stored
    assert 'alt="Written by hand"' in stored  # a text written by hand is left alone
    page.refresh_from_db()
    assert {alt.file_name(i["src"]): i["alt"] for i in page.images}["team.jpg"] == (
        'Team "A" planning a backup'
    )
    assert page.stats["images_missing_alt"] == 0
    change = api.get().json()["changes"][0]
    assert api.post(f"/changes/{change['id']}/rollback").status_code == 200
    stored = next(r for r in wp.rows["page"] if r["id"] == 11)["content"]
    assert 'class="wp-image-201 size-full" src="' in stored
    assert 'alt="Team' not in stored


NO_CLASS = (
    '<h2>Backup</h2><img src="{base}/wp-content/uploads/team-1024x487.jpg" alt="">'
    '<img src="{base}/wp-content/uploads/unknown.jpg" alt="">'
)


def test_pictures_without_an_id_are_matched_by_file_name(api, page, wp):
    wp.edit("page", 11, content=NO_CLASS.format(base=BASE))
    script_llm(
        reply(
            {"alt": "Team planning a backup", "decorative": False, "note": ""},
            {"alt": "Unknown", "decorative": False, "note": ""},
        )
    )
    api.post("/refresh")
    found = api.post("/alt/suggest", {}).json()
    by_file = {alt.file_name(f["src"]): f["attachment_id"] for f in found}
    assert by_file == {"team-1024x487.jpg": 201, "unknown.jpg": None}  # no library file: refused
    response = api.post("/alt/apply", {"alts": {"201": "Team planning a backup"}})
    assert response.status_code == 200, response.content
    stored = next(r for r in wp.rows["page"] if r["id"] == 11)["content"]
    assert 'team-1024x487.jpg" alt="Team planning a backup"' in stored
    assert 'unknown.jpg" alt=""' in stored


def test_a_file_name_that_matches_two_library_files_is_refused(page, wp):
    wp.media[301] = {"alt": "", "type": "image/jpeg", "file": "team.jpg"}
    wp.edit("page", 11, content=NO_CLASS.format(base=BASE))
    page.images = [{"src": f"{BASE}/wp-content/uploads/team-1024x487.jpg", "alt": ""}]
    assert alt.resolve_attachments(page) is False
    assert page.images[0].get("attachment_id") is None


def test_rollback_restores_the_previous_alt_text(api, wp):
    api.post("/alt/apply", {"alts": {"201": "Something new"}})
    change = api.get().json()["changes"][0]
    assert api.post(f"/changes/{change['id']}/rollback").status_code == 200
    assert wp.media[201]["alt"] == ""


def test_pictures_outside_the_media_library_and_empty_texts_are_refused(api, wp):
    assert api.post("/alt/apply", {"alts": {"999": "Unknown image"}}).status_code == 422
    assert api.post("/alt/apply", {"alts": {"201": "   "}}).status_code == 422
    assert wp.posted == []


def test_too_long_alt_text_is_refused(api):
    assert api.post("/alt/apply", {"alts": {"201": "x" * 300}}).status_code in {400, 422}


def test_refresh_reads_the_page_again(api, page, wp):
    wp.media[201]["alt"] = "Changed on the site"
    response = api.post("/refresh")
    assert response.status_code == 200, response.content
    alts = {alt.file_name(i["src"]): i["alt"] for i in response.json()["item"]["images"]}
    assert alts["team.jpg"] == "Changed on the site"


def test_viewers_cannot_change_alt_text(client, workspace, synced, page):
    viewer = make_user("alt-viewer@example.com", workspace, Role.VIEWER)
    client.force_login(viewer)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{synced.pk}/pages/{page.pk}"
    bodies = {"/alt/suggest": {}, "/alt/apply": {"alts": {"201": "x"}}, "/refresh": {}}
    for path, body in bodies.items():
        response = client.post(base + path, data=body, content_type=JSON, **headers)
        assert response.status_code == 403, path


def test_base_constant_is_the_site_origin():
    assert BASE.startswith("https://")


def test_a_newer_alt_change_replaces_an_older_one_that_has_not_started(api, page, wp):
    from content.actions import MEDIA_KIND

    old = ProposedAction.objects.create(
        workspace_id=page.workspace_id,
        kind=MEDIA_KIND,
        payload={
            "item_id": str(page.pk),
            "website_id": str(page.website_id),
            "alts": {"201": "old"},
        },
        risk="r2",
        status=ActionStatus.APPROVED,
        requested_by="user:1",
        idempotency_key="old-alt-change",
    )
    response = api.post("/alt/apply", {"alts": {"201": "The newer text"}})
    assert response.status_code == 200, response.content
    old.refresh_from_db()
    assert old.status == ActionStatus.EXPIRED
    assert old.error == "superseded"
    assert wp.media[201]["alt"] == "The newer text"
