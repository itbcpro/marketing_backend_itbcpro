import pytest

from content import body_html, improve
from content.models import PostRevision, RevisionStatus
from content.tasks import improve_post
from content.tests.conftest import script_llm
from content.tests.fake_wordpress import BASE
from core.errors import ValidationFailedError

JSON = "application/json"
ABOUT = f"{BASE}/about/"


def rewrite(raw, *, add="<h2>A new heading</h2><p>More detail.</p>", changes=None):
    return {
        "html": raw + add,
        "changes": changes or [{"what": "Added a heading", "why": "Easier to scan"}],
        "notes": "",
    }


def raw_of(wp):
    return wp.rows["post"][0]["content"]


def make_revision(post, focus=("headings",)):
    return improve.start(post, requested_by="user:1", focus=list(focus))


# ---- the sanitiser ----


def test_clean_html_keeps_content_and_drops_anything_dangerous():
    dirty = (
        '<div class="box" onclick="x()"><h1>Title</h1><p style="color:red">Hi '
        '<a href="javascript:alert(1)">bad</a> <a href="https://a.example/x" target="_blank">ok</a>'
        '<script>alert(1)</script></p><iframe src="https://evil.example"></iframe>'
        '<img src="/a.png" alt="A" onerror="x()"></div>'
    )
    out = body_html.clean_html(dirty)
    assert "<script" not in out and "<iframe" not in out and "onclick" not in out
    assert "onerror" not in out and "javascript:" not in out and "alert(1)" not in out
    assert "<h1>Title</h1>" in out  # headings that exist stay as they are
    assert 'class="box"' in out
    assert 'href="https://a.example/x"' in out and 'rel="noopener"' in out
    assert '<img src="/a.png" alt="A">' in out


def test_extra_h1s_added_by_a_rewrite_become_h2():
    assert body_html.demote_new_h1("<p>a</p>", "<h1>New</h1><p>a</p>") == "<h2>New</h2><p>a</p>"
    kept = "<h1>Old</h1><p>a</p>"
    assert body_html.demote_new_h1(kept, kept) == kept


def test_clean_html_closes_tags_and_escapes_text():
    assert body_html.clean_html("<p>1 < 2 <b>bold") == "<p>1 &lt; 2 <b>bold</b></p>"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("<p>hi</p><iframe src='x'></iframe>", "special_markup"),
        ("<!-- wp:paragraph --><p>hi</p><!-- /wp:paragraph -->", "special_markup"),
        ("<p>[contact-form-7 id=1]</p>", "shortcodes"),
        ("<p>Plain [1] reference</p>", ""),
    ],
)
def test_risky_bodies_are_recognised(source, reason):
    assert body_html.risky_reason(source) == reason


def test_lost_assets_lists_removed_links_and_images():
    before = '<a href="/a/">a</a><img src="x.png"><a href="/b/">b</a>'
    after = '<a href="/a/">a</a><a href="/c/">c</a>'
    assert body_html.lost_assets(before, after) == ["/b/", "x.png"]
    assert body_html.new_links(before, after) == {"/c/"}


# ---- the rewrite ----


def test_a_good_rewrite_is_kept_for_review(post, wp):
    revision = make_revision(post)
    link = f'<p>See <a href="{ABOUT}">our about page</a>.</p>'
    script_llm(rewrite(raw_of(wp), add="<h2>A new heading</h2>" + link))
    done = improve.run(revision)
    assert done.status == RevisionStatus.DONE
    assert done.before_html == raw_of(wp)
    assert "<h2>A new heading</h2>" in done.after_html
    assert done.stats["headings_after"] == done.stats["headings_before"] + 1
    assert done.stats["added_links"] == [ABOUT]
    assert done.stats["after_words"] > done.stats["before_words"]
    assert done.base_modified_at


def test_links_the_model_invented_are_removed(post, wp):
    revision = make_revision(post)
    bad = '<p>Read <a href="https://evil.example/x">this</a>.</p>'
    script_llm(rewrite(raw_of(wp), add=bad))
    done = improve.run(revision)
    assert done.status == RevisionStatus.DONE
    assert "evil.example" not in done.after_html and "Read this." in done.after_html


def test_a_rewrite_that_drops_links_or_text_is_refused(post, wp):
    revision = make_revision(post)
    script_llm({"html": "<p>Tiny.</p>", "changes": [], "notes": ""})
    assert improve.run(revision).error == "rewrite_too_short"
    keep_length = raw_of(wp).replace('<a href="/about/">about</a>', "about")
    script_llm({"html": keep_length, "changes": [], "notes": ""})
    assert improve.run(make_revision(post)).error == "rewrite_lost_links"


def test_posts_built_with_elementor_or_special_markup_are_not_rewritten(post, wp):
    wp.rows["post"][0]["_elementor_data"] = "[]x"
    assert improve.run(make_revision(post)).error == "page_builder_post"
    wp.rows["post"][0]["_elementor_data"] = ""
    wp.edit("post", 1, content="<p>Hello</p><iframe src='x'></iframe>")
    assert improve.run(make_revision(post)).error == "special_markup"


def test_only_wordpress_posts_can_be_improved(post):
    post.source = "sitemap"
    with pytest.raises(ValidationFailedError):
        improve.start(post, requested_by="u")


def test_unknown_focus_keys_are_dropped(post):
    assert make_revision(post, focus=("headings", "seo_title", "nonsense")).focus == ["headings"]


# ---- applying through the API ----


@pytest.fixture
def api(client, owner, workspace, synced, post, django_capture_on_commit_callbacks):
    client.force_login(owner)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{synced.pk}/pages/{post.pk}"

    class Api:
        def __init__(self):
            self.base = base

        def get(self, path=""):
            return client.get(base + path, **headers)

        def post(self, path, body=None):
            with django_capture_on_commit_callbacks(execute=True):
                return client.post(base + path, data=body or {}, content_type=JSON, **headers)

    return Api()


def test_start_apply_and_roll_back(api, post, wp, monkeypatch):
    queued = []
    monkeypatch.setattr(improve_post, "delay", queued.append)
    started = api.post("/improve-body", {"focus": ["headings"], "instruction": "keep it short"})
    assert started.status_code == 200 and queued == [started.json()["id"]]
    assert api.post("/improve-body", {}).json()["id"] == started.json()["id"]  # one at a time
    original = raw_of(wp)
    script_llm(rewrite(original))
    improve.run(PostRevision.objects.get(pk=started.json()["id"]))

    shown = api.get("/revision").json()
    assert shown["status"] == "done" and "A new heading" in shown["after_preview"]
    assert shown["changes"][0]["what"] == "Added a heading"

    applied = api.post(f"/revision/{shown['id']}/apply")
    assert applied.status_code == 200
    assert "A new heading" in raw_of(wp)
    assert api.get("/revision").json()["status"] == "applied"
    change = next(c for c in api.get().json()["changes"] if c["summary"].startswith("Improve"))
    assert change["status"] in {"succeeded", "verified"} and change["can_rollback"]

    undone = api.post(f"/changes/{change['id']}/rollback")
    assert undone.status_code == 200
    assert raw_of(wp) == original


def test_apply_is_refused_when_the_post_changed_on_the_site(api, post, wp):
    revision = make_revision(post)
    script_llm(rewrite(raw_of(wp)))
    improve.run(revision)
    wp.edit("post", 1, content=raw_of(wp) + "<p>Edited in WordPress.</p>")
    api.post(f"/revision/{revision.pk}/apply")
    assert "Edited in WordPress" in raw_of(wp)
    assert "A new heading" not in raw_of(wp)


def test_dismiss_and_unfinished_rewrites(api, post, wp):
    revision = make_revision(post)
    assert api.post(f"/revision/{revision.pk}/apply").status_code == 422
    script_llm(rewrite(raw_of(wp)))
    improve.run(revision)
    assert api.post(f"/revision/{revision.pk}/dismiss").json()["status"] == "dismissed"
    assert api.get("/revision").json() is None
