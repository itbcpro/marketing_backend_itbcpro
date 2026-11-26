import io
import itertools
import json

import pytest
from PIL import Image

from actions.models import ActionStatus
from content.blog import images, openai_images, render, writer
from content.models import Article, ArticleStatus
from content.tasks import write_article
from content.tests.conftest import script_llm
from content.tests.fake_wordpress import BASE
from content.tests.test_blog import (
    JSON,
    audit_payload,
    faq_payload,
    plan_payload,
    section_payload,
)
from core.errors import PermanentProviderError, ValidationFailedError
from media.models import MediaAsset

IMAGE_PLAN = [
    {"placement": "Part 2", "concept": "A shield", "alt": "inline alt", "caption": "inline cap"},
    {"placement": "featured", "concept": "A desk", "alt": "hero alt", "caption": ""},
]


def image_prompt(name):
    return {
        "prompt": "A calm desk scene.",
        "filename": name,
        "alt": f"{name} alt",
        "caption": f"{name} caption",
    }


COUNTER = itertools.count(1)


def webp_bytes():
    """A different picture on every call, as a real generator would produce."""
    shade = next(COUNTER) * 37 % 250
    out = io.BytesIO()
    Image.new("RGB", (64, 40), (shade, 90, 160)).save(out, "WEBP", lossless=True)
    return out.getvalue()


def draw(workspace_id, prompt, shape="landscape"):
    return webp_bytes()


def refuse(workspace_id, prompt, shape="landscape"):
    raise PermanentProviderError("image_refused", provider="openai_images")


@pytest.fixture
def image_key(monkeypatch):
    monkeypatch.setattr(openai_images, "has_key", lambda workspace_id: True)
    monkeypatch.setattr(openai_images, "generate", draw)


def finish(article):
    while writer.advance(article):
        article.refresh_from_db()
    article.refresh_from_db()
    return article


def make_ready(synced, *, with_images=True):
    payloads = [plan_payload(3, image_plan=IMAGE_PLAN)]
    payloads += [section_payload(f"Part {i + 1}") for i in range(3)] + [faq_payload()]
    if with_images:
        payloads += [image_prompt("hero-desk"), image_prompt("shield-part")]
    payloads.append(audit_payload())
    script_llm(*payloads)
    article = writer.start_article(
        synced,
        requested_by="user:1",
        title="Managed IT cost",
        primary_keyword="managed it support cost",
        blog_type="ultimate_guide",
        min_words=800,
    )
    return finish(article)


# ---- rendering ----


def test_markdown_is_escaped_and_only_http_links_survive():
    out = render.markdown_to_html(
        "Hello <script>alert(1)</script> **bold** [ok](https://a.example/x) "
        "[bad](javascript:alert(1))\n\n- one\n- two\n\n1. a\n2. b"
    )
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert '<a href="https://a.example/x">ok</a>' in out
    assert '<a href="javascript' not in out
    assert "<ul><li>one</li><li>two</li></ul>" in out
    assert "<ol><li>a</li><li>b</li></ol>" in out


def test_markdown_tables_become_html_tables():
    out = render.markdown_to_html("| A | B |\n| --- | --- |\n| 1 | 2 |")
    assert "<th>A</th><th>B</th>" in out
    assert "<td>1</td><td>2</td>" in out


def test_faq_jsonld_cannot_close_the_script_tag():
    article = Article(faq=[{"question": "Q?", "answer": "A </script><b>x</b>"}], plan={})
    data = render.faq_jsonld(article)
    assert "</script>" not in data
    assert json.loads(data.replace("<\\/", "</"))["@type"] == "FAQPage"


def test_elementor_layout_has_sections_images_and_faq(synced, image_key):
    article = make_ready(synced)
    for entry in article.images:
        entry["wp_media_id"], entry["wp_url"] = 900, f"{BASE}/wp-content/uploads/x.webp"
    rows = render.build_elementor(article)
    assert len(rows) == 4  # three sections and the FAQ
    assert rows[0]["elements"][0]["elements"][0]["widgetType"] == "html"  # the article styles
    assert {r["settings"]["css_classes"] for r in rows} == {"itbcpro-article"}
    kinds = [w["widgetType"] for w in rows[1]["elements"][0]["elements"]]
    assert kinds == ["heading", "image", "text-editor"]  # the inline image sits under "Part 2"
    faq_kinds = [w["widgetType"] for w in rows[-1]["elements"][0]["elements"]]
    assert faq_kinds == ["heading", "accordion", "html"]
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids)
    assert "<h2>Part 1</h2>" in render.build_html(article)


# ---- images ----


def test_images_are_drawn_stored_and_described(synced, image_key):
    article = make_ready(synced)
    assert article.status == ArticleStatus.READY
    assert [e["status"] for e in article.images] == ["ready", "ready"]
    assert article.images[0]["placement"] == "featured"  # featured image first
    asset = MediaAsset.objects.get(pk=article.images[0]["asset_id"])
    assert asset.ai_generated and asset.alt_text == "hero-desk alt"
    assert article.images[0]["filename"] == "hero-desk.webp"
    assert images.ready_count(article) == 2


def test_without_an_openai_key_images_stay_planned_and_the_article_finishes(synced):
    article = make_ready(synced, with_images=False)
    assert article.status == ArticleStatus.READY
    assert [e["status"] for e in article.images] == ["planned", "planned"]


def test_a_refused_image_is_recorded_and_can_be_redrawn(synced, image_key, monkeypatch):
    monkeypatch.setattr(openai_images, "generate", refuse)
    article = make_ready(synced)
    assert article.status == ArticleStatus.READY
    assert {e["status"] for e in article.images} == {"failed"}
    monkeypatch.setattr(openai_images, "generate", draw)
    script_llm(image_prompt("hero-desk"), image_prompt("shield-part"))
    writer.redraw(article, [])
    assert article.status == ArticleStatus.QUEUED
    finish(article)
    assert article.status == ArticleStatus.READY
    assert {e["status"] for e in article.images} == {"ready"}


def test_redraw_refuses_a_missing_key_and_a_busy_article(synced, image_key):
    article = make_ready(synced)
    article.status = ArticleStatus.WRITING
    with pytest.raises(ValidationFailedError):
        writer.redraw(article, [])
    article.status = ArticleStatus.READY
    with pytest.raises(ValidationFailedError):
        writer.redraw(article, ["no-such-image"])


# ---- publishing through the action layer ----


@pytest.fixture
def pub(client, owner, workspace, synced, wp, image_key, django_capture_on_commit_callbacks):
    article = make_ready(synced)
    client.force_login(owner)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{synced.pk}/blog/articles/{article.pk}"

    class Api:
        def __init__(self):
            self.article, self.wp = article, wp

        def get(self, path=""):
            return client.get(base + path, **headers)

        def post(self, path, body=None):
            with django_capture_on_commit_callbacks(execute=True):
                return client.post(base + path, data=body or {}, content_type=JSON, **headers)

        def reload(self):
            self.article.refresh_from_db()
            return self.article

    return Api()


def test_publish_info_reports_what_the_site_allows(pub):
    info = pub.get("/publish-info").json()
    assert info["connected"] and info["elementor"] and info["seo_fields"]
    assert info["elementor_version"] == "3.25.4"
    assert (info["images_ready"], info["images_total"]) == (2, 2)
    assert info["snippet"] == ""


def test_publish_info_gives_the_snippet_when_elementor_is_not_writable(pub):
    pub.wp.elementor_exposed = False
    info = pub.get("/publish-info").json()
    assert not info["elementor"]
    assert "register_post_meta" in info["snippet"]


def test_send_draft_uploads_images_and_writes_elementor_and_seo_data(pub):
    response = pub.post("/publish", {"status": "draft"})
    assert response.status_code == 200
    article = pub.reload()
    assert article.wp_post_id and article.wp_status == "draft"
    assert [u["file"] for u in pub.wp.uploads] == ["hero-desk.webp", "shield-part.webp"]
    assert all(e["wp_media_id"] for e in article.images)
    body = pub.wp.created[0]
    assert body["status"] == "draft"
    assert body["featured_media"] == article.images[0]["wp_media_id"]
    assert body["meta"]["_elementor_edit_mode"] == "builder"
    assert body["meta"]["_elementor_version"] == "3.25.4"
    layout = json.loads(body["meta"]["_elementor_data"])
    assert layout[0]["elType"] == "section"
    assert body["meta"]["rank_math_title"] == article.meta["seo_title"]
    assert "<h2>Part 1</h2>" in body["content"]
    state = pub.get().json()["publish_state"]
    assert state["status"] in {ActionStatus.SUCCEEDED, ActionStatus.VERIFIED}
    assert state["result"]["elementor"] is True


def test_without_the_snippet_the_draft_is_html_only(pub):
    pub.wp.elementor_exposed = False
    pub.post("/publish", {"status": "draft"})
    body = pub.wp.created[0]
    assert "_elementor_data" not in body["meta"]
    assert body["content"].startswith("<h2>")


def test_going_live_updates_the_same_post_then_it_is_locked(pub):
    pub.post("/publish", {"status": "draft"})
    first_post = pub.reload().wp_post_id
    pub.post("/publish", {"status": "publish"})
    article = pub.reload()
    assert article.wp_post_id == first_post and article.wp_status == "publish"
    assert len(pub.wp.created) == 1
    assert len(pub.wp.uploads) == 2  # images are uploaded once
    assert pub.post("/publish", {"status": "draft"}).status_code == 422
    assert pub.post("/trash").status_code == 422


def test_publishing_before_a_draft_exists_and_bad_schedules_are_refused(pub):
    assert pub.post("/publish", {"status": "publish"}).status_code == 422
    pub.post("/publish", {"status": "draft"})
    assert pub.post("/publish", {"status": "future"}).status_code == 422
    ok = pub.post("/publish", {"status": "future", "schedule_at": "2026-12-01T09:00:00"})
    assert ok.status_code == 200
    assert pub.reload().wp_status == "future"


def test_trash_moves_the_draft_to_the_wordpress_trash(pub):
    pub.post("/publish", {"status": "draft"})
    post_id = pub.reload().wp_post_id
    assert pub.post("/trash").status_code == 200
    article = pub.reload()
    assert article.wp_post_id is None and article.wp_status == "trash"
    row = next(r for r in pub.wp.rows["post"] if r["id"] == post_id)
    assert row["status"] == "trash"


def test_redraw_endpoint_queues_the_worker(pub, monkeypatch):
    queued = []
    monkeypatch.setattr(write_article, "delay", queued.append)
    response = pub.post("/images/redraw", {"image_ids": ["img1"]})
    assert response.status_code == 200
    assert queued == [str(pub.article.pk)]
    assert pub.reload().images[0]["status"] == "queued"


def test_an_article_planned_before_images_existed_can_still_draw_them(synced, monkeypatch):
    article = make_ready(synced, with_images=False)  # no key yet
    article.images = []
    article.save()
    monkeypatch.setattr(openai_images, "has_key", lambda workspace_id: True)
    monkeypatch.setattr(openai_images, "generate", draw)
    script_llm(image_prompt("hero-desk"), image_prompt("shield-part"))
    writer.redraw(article, [])
    finish(article)
    assert [e["status"] for e in article.images] == ["ready", "ready"]


def test_no_openai_credits_stops_after_the_first_image_with_a_clear_code(synced, monkeypatch):
    calls = []

    def broke(workspace_id, prompt, shape="landscape"):
        calls.append(prompt)
        raise PermanentProviderError(openai_images.NO_CREDITS, provider="openai_images")

    monkeypatch.setattr(openai_images, "has_key", lambda workspace_id: True)
    monkeypatch.setattr(openai_images, "generate", broke)
    script_llm(
        plan_payload(3, image_plan=IMAGE_PLAN),
        *[section_payload(f"Part {i + 1}") for i in range(3)],
        faq_payload(),
        image_prompt("hero-desk"),
        audit_payload(),
    )
    article = writer.start_article(
        synced, requested_by="u", title="t", primary_keyword="k", blog_type="how_to", min_words=800
    )
    finish(article)
    assert article.status == ArticleStatus.READY
    assert len(calls) == 1
    assert {e["error"] for e in article.images} == {openai_images.NO_CREDITS}
    assert {e["status"] for e in article.images} == {"failed"}


def test_the_credit_error_is_recognised_from_the_openai_reply(workspace, monkeypatch):
    import httpx

    from content import net

    body = {"error": {"type": "insufficient_quota", "code": "credit_balance_exhausted"}}
    monkeypatch.setattr(
        net, "transport", httpx.MockTransport(lambda request: httpx.Response(429, json=body))
    )
    monkeypatch.setattr(net, "resolver", lambda host, port: ["93.184.216.34"])
    with pytest.raises(PermanentProviderError, match=openai_images.NO_CREDITS):
        openai_images._call("POST", "/images/generations", "sk-x", {})


def test_images_are_compressed_before_they_are_stored():
    from PIL import Image as PilImage

    big = io.BytesIO()
    noise = PilImage.effect_noise((3000, 2000), 80).convert("RGB")
    noise.save(big, "WEBP", lossless=True)
    small = images.compress(big.getvalue())
    assert len(small) < len(big.getvalue())
    with PilImage.open(io.BytesIO(small)) as result:
        assert result.width == images.MAX_WIDTH and result.format == "WEBP"


def test_sending_again_after_a_failure_starts_a_new_action(pub):
    from actions.models import ProposedAction

    pub.post("/publish", {"status": "draft"})
    first = ProposedAction.objects.get(kind="content.publish_article")
    # pretend the first send failed before it created the post
    ProposedAction.objects.filter(pk=first.pk).update(status=ActionStatus.FAILED_FINAL)
    article = pub.reload()
    article.wp_post_id, article.wp_status = None, ""
    article.save()
    again = pub.post("/publish", {"status": "draft"})
    assert again.status_code == 200
    assert again.json()["action_id"] != str(first.pk)
    assert ProposedAction.objects.filter(kind="content.publish_article").count() == 2


def test_status_is_small_and_changes_when_the_publish_state_changes(pub):
    before = pub.get("/status").json()
    assert before["status"] == "ready" and before["publish_status"] == ""
    assert len(pub.get("/status").content) < 300  # small enough to poll
    pub.post("/publish", {"status": "draft"})
    after = pub.get("/status").json()
    assert after["wp_status"] == "draft"
    assert after["signature"] != before["signature"]
