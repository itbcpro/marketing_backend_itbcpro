import pytest

from content.blog import gate, kinds, topics, weights, writer
from content.blog.schemas import CategoryScore
from content.models import Article, ArticleIdea, ArticleStatus, IdeaStatus
from content.tasks import write_article
from content.tests.conftest import script_llm
from content.tests.fake_wordpress import BASE
from core.errors import ValidationFailedError

JSON = "application/json"
PARAGRAPH = (
    "Managed IT support keeps small business systems patched, backed up and monitored so staff "
    "can keep working. A good provider explains what it does, how fast it responds and what it "
    "costs, and it documents every system it touches. "
)


def idea_payload(**over):
    base = {
        "title": "Managed IT Support Costs for Small Business",
        "primary_keyword": "managed it support cost",
        "secondary_keywords": ["it support pricing"],
        "intent": "commercial",
        "blog_type": "ultimate_guide",
        "angle": "Real cost drivers, not a price list.",
        "why_now": "Renewal season.",
        "evidence": "No post covers cost.",
        "cluster": "managed it",
        "priority": "high",
        "overlaps": [f"{BASE}/hello-world/", "https://elsewhere.example/post"],
    }
    base.update(over)
    return base


def plan_payload(sections=3, words=300, image_plan=()):
    return {
        "intent": "Understand what managed IT support costs.",
        "audience": "Owners of small businesses.",
        "information_gap": "Competitors list prices but not the drivers.",
        "h1": "Managed IT Support Cost: What Small Businesses Really Pay",
        "seo_title": "Managed IT Support Cost for Small Business",
        "meta_description": "What drives the cost of managed IT support, how pricing models differ "
        "and how a small business can budget without surprises.",
        "slug": "Managed IT Support Cost!",
        "key_takeaways": ["Price follows scope."],
        "sections": [
            {
                "heading": f"Part {i + 1}",
                "purpose": "Explain.",
                "key_points": ["a", "b"],
                "target_words": words,
                "format": "prose",
                "subheadings": [],
                "direct_answer_question": "",
                "link_to": [f"{BASE}/hello-world/", "https://invented.example/x"] if i == 0 else [],
            }
            for i in range(sections)
        ],
        "faq_questions": [f"Question {i}?" for i in range(22)],
        "entities": ["MSP"],
        "needs_source": ["average hourly rate"],
        "image_plan": list(image_plan),
        "schema_types": ["BlogPosting"],
        "cta": "Book a call.",
        "freshness_notes": "Prices change yearly.",
    }


def section_payload(heading, words=300):
    text = (PARAGRAPH * (words // 30 + 1)).strip()
    return {
        "markdown": f"## {heading}\n\n{text}",
        "summary": f"{heading} done.",
        "open_claims": [],
    }


def faq_payload(count=20):
    answer = (
        "Managed IT support is a monthly service that covers monitoring, patching and help. " * 3
    )
    return {"items": [{"question": f"Question {i}?", "answer": answer} for i in range(count)]}


def audit_payload(score=80):
    return {
        "scores": [{"category": key, "score": score, "note": "ok"} for key in weights.WEIGHTS],
        "strengths": ["Clear"],
        "weaknesses": [{"severity": "low", "where": "Part 1", "fix": "Add a table."}],
        "unsupported_claims": [],
        "content_gap": {"covered_better": "a", "competitors_often_miss": "b", "added_value": "c"},
        "improvements": ["Add images."],
        "update_schedule": "Yearly.",
        "structured_data": [{"type": "BlogPosting", "reason": "It is a blog post."}],
        "verdict": {"value": "publish_after_fixes", "reason": "Needs sources."},
    }


def full_script(sections=3):
    return [
        plan_payload(sections),
        *[section_payload(f"Part {i + 1}") for i in range(sections)],
        faq_payload(),
        audit_payload(),
    ]


def run_to_end(article):
    steps = 0
    while writer.advance(article):
        steps += 1
        article.refresh_from_db()
    article.refresh_from_db()
    return steps


# ---- topics ----


def test_suggest_stores_a_batch_and_drops_unknown_overlaps(synced):
    script_llm({"ideas": [idea_payload(), idea_payload(title="Another topic", blog_type="bogus")]})
    ideas = topics.suggest(synced, requested_by="user:1", count=5)
    assert [i.title for i in ideas] == [
        "Managed IT Support Costs for Small Business",
        "Another topic",
    ]
    assert ideas[0].overlaps == [f"{BASE}/hello-world/"]
    assert ideas[1].blog_type == kinds.DEFAULT_TYPE
    assert len({i.batch for i in ideas}) == 1


def test_suggest_forces_the_chosen_blog_type_and_skips_repeats(synced):
    script_llm({"ideas": [idea_payload()]}, {"ideas": [idea_payload(), idea_payload(title="New")]})
    topics.suggest(synced, requested_by="user:1", blog_type="how_to")
    second = topics.suggest(synced, requested_by="user:1", blog_type="how_to")
    assert [i.title for i in second] == ["New"]
    assert {i.blog_type for i in ArticleIdea.objects.all()} == {"how_to"}


def test_suggest_tells_the_model_about_existing_posts_and_missing_search_data(synced):
    gateway = script_llm({"ideas": [idea_payload()]})
    topics.suggest(synced, requested_by="user:1", focus="pricing pages")
    system = gateway.requests[0][0].system[0].text
    assert "Hello World" in system
    assert "no search data" in system
    assert "pricing pages" in system


def test_unknown_blog_type_is_refused(synced):
    with pytest.raises(ValidationFailedError):
        topics.suggest(synced, requested_by="user:1", blog_type="bogus")


def test_every_blog_type_has_guidance():
    assert all(kinds.guidance(key) for key in kinds.BLOG_TYPES)


# ---- writing ----


def test_article_is_written_step_by_step_and_scored(synced):
    script_llm(*full_script())
    article = writer.start_article(
        synced,
        requested_by="user:1",
        title="Managed IT cost",
        primary_keyword="managed it support cost",
        blog_type="ultimate_guide",
        min_words=800,
    )
    assert run_to_end(article) == 5
    assert article.status == ArticleStatus.READY
    assert [s["heading"] for s in article.sections] == ["Part 1", "Part 2", "Part 3"]
    assert len(article.faq) == 20
    assert article.meta["slug"] == "managed-it-support-cost"
    assert 0 < article.score <= 80
    assert article.word_count > 800
    assert writer.progress(article) == 100


def test_plan_keeps_only_known_links_and_scales_word_targets(synced):
    script_llm(plan_payload(sections=4, words=300))
    article = writer.start_article(
        synced,
        requested_by="user:1",
        title="t",
        primary_keyword="k",
        blog_type="how_to",
        min_words=8000,
    )
    writer.advance(article)
    sections = article.plan["sections"]
    assert sections[0]["link_to"] == [f"{BASE}/hello-world/"]
    assert sum(s["target_words"] for s in sections) >= 8000
    assert article.step == "section:0"


def test_a_failed_step_marks_the_article_and_retry_continues_there(synced):
    from llm.fake import FakeGateway, text_response
    from llm.service import LlmService, set_llm_service

    set_llm_service(LlmService(FakeGateway([text_response("not json")] * 3)))
    article = writer.start_article(
        synced,
        requested_by="user:1",
        title="t",
        primary_keyword="k",
        blog_type="how_to",
        min_words=800,
    )
    assert writer.advance(article) is False
    assert article.status == ArticleStatus.FAILED
    assert article.step == "plan"
    script_llm(*full_script())
    writer.retry(article)
    assert run_to_end(article) == 5
    assert article.status == ArticleStatus.READY


def test_idea_status_follows_the_article(synced):
    script_llm({"ideas": [idea_payload()]}, *full_script())
    idea = topics.suggest(synced, requested_by="user:1")[0]
    article = writer.start_article(synced, requested_by="user:1", idea=idea, min_words=800)
    idea.refresh_from_db()
    assert idea.status == IdeaStatus.WRITING
    article.idea = idea
    run_to_end(article)
    idea.refresh_from_db()
    assert idea.status == IdeaStatus.WRITTEN


def test_start_validates_inputs(synced):
    with pytest.raises(ValidationFailedError):
        writer.start_article(synced, requested_by="u", title="t", primary_keyword="k")
    with pytest.raises(ValidationFailedError):
        writer.start_article(
            synced,
            requested_by="u",
            title="t",
            primary_keyword="k",
            blog_type="how_to",
            min_words=5,
        )


def test_task_chains_the_next_step(synced, monkeypatch):
    script_llm(plan_payload())
    article = writer.start_article(
        synced, requested_by="u", title="t", primary_keyword="k", blog_type="how_to", min_words=800
    )
    queued = []
    monkeypatch.setattr(write_article, "delay", queued.append)
    write_article.run(str(article.pk))
    assert queued == [str(article.pk)]


# ---- gate and score ----


def make_article(synced, **over):
    data = {
        "workspace_id": synced.workspace_id,
        "website": synced,
        "title": "t",
        "primary_keyword": "managed it support",
        "blog_type": "how_to",
        "min_words": 800,
        "meta": {
            "h1": "Managed IT support explained",
            "seo_title": "Managed IT support explained",
            "meta_description": "x" * 140,
            "slug": "managed-it-support",
        },
        "sections": [
            {
                "heading": f"H{i}",
                "markdown": f"## Managed IT support {i}\n\n{PARAGRAPH * 8}",
                "words": 300,
                "target": 300,
            }
            for i in range(6)
        ],
        "faq": [{"question": f"Q{i}?", "answer": "word " * 60} for i in range(20)],
    }
    data.update(over)
    return Article(**data)


def test_gate_passes_a_good_article(synced):
    result = gate.run(make_article(synced), {f"{BASE}/hello-world/"}, "wp.example.com")
    assert result["passed"], result["failed"]
    assert result["word_count"] >= 800


def test_gate_fails_short_text_few_faq_and_invented_internal_links(synced):
    article = make_article(
        synced,
        sections=[
            {
                "heading": "H",
                "markdown": "## H\n\nFurthermore [x](https://wp.example.com/made-up/) short.",
                "words": 5,
                "target": 300,
            }
        ],
        faq=[],
    )
    result = gate.run(article, {f"{BASE}/hello-world/"}, "wp.example.com")
    assert not result["passed"]
    assert {"word_count", "faq_count", "links_only_known_pages"} <= set(result["failed"])
    banned = next(c for c in result["checks"] if c["key"] == "no_banned_phrases")
    assert banned["status"] == "warn"


def test_score_is_weighted_and_capped_by_failed_checks():
    scores = [CategoryScore(category=k, score=90, note="") for k in weights.WEIGHTS]
    assert weights.total(scores, []) == 90
    assert weights.total(scores, ["word_count", "faq_count"]) == 80
    assert weights.total(scores[:1], []) < 20


# ---- API ----


@pytest.fixture
def api(client, owner, workspace, synced, django_capture_on_commit_callbacks):
    client.force_login(owner)
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    base = f"/api/v1/websites/{synced.pk}/blog"

    class Api:
        site = synced

        def get(self, path):
            return client.get(base + path, **headers)

        def post(self, path, body=None):
            with django_capture_on_commit_callbacks(execute=True):
                return client.post(base + path, data=body or {}, content_type=JSON, **headers)

        def put(self, path, body):
            return client.put(base + path, data=body, content_type=JSON, **headers)

        def delete(self, path):
            return client.delete(base + path, **headers)

    return Api()


def test_api_suggest_choose_type_write_and_read(api, monkeypatch):
    queued = []
    monkeypatch.setattr(write_article, "delay", queued.append)
    script_llm({"ideas": [idea_payload()]})
    ideas = api.post("/ideas/suggest", {"count": 3}).json()
    assert ideas[0]["status"] == "suggested"
    changed = api.put(f"/ideas/{ideas[0]['id']}", {"blog_type": "comparison"}).json()
    assert changed["blog_type"] == "comparison"
    made = api.post("/articles", {"idea_id": ideas[0]["id"], "min_words": 800})
    assert made.status_code == 200
    row = made.json()
    assert row["blog_type"] == "comparison"
    assert row["status"] == "queued"
    assert queued == [row["id"]]
    again = api.post("/articles", {"idea_id": ideas[0]["id"]})
    assert again.status_code == 422
    detail = api.get(f"/articles/{row['id']}").json()
    assert detail["plan"] == {}
    assert [a["id"] for a in api.get("/articles").json()] == [row["id"]]


def test_api_rejects_unknown_type_and_hides_dismissed_ideas(api):
    assert (
        api.put("/ideas/00000000-0000-0000-0000-000000000000", {"blog_type": "x"}).status_code
        == 404
    )
    script_llm({"ideas": [idea_payload()]})
    idea = api.post("/ideas/suggest", {}).json()[0]
    assert api.put(f"/ideas/{idea['id']}", {"blog_type": "bogus"}).status_code == 422
    api.post(f"/ideas/{idea['id']}/dismiss")
    assert api.get("/ideas").json() == []


def test_api_delete_refuses_a_busy_article_and_frees_the_idea(api, synced, monkeypatch):
    monkeypatch.setattr(write_article, "delay", lambda pk: None)
    script_llm({"ideas": [idea_payload()]})
    idea = api.post("/ideas/suggest", {}).json()[0]
    row = api.post("/articles", {"idea_id": idea["id"], "min_words": 800}).json()
    assert api.delete(f"/articles/{row['id']}").status_code == 422
    Article.objects.filter(pk=row["id"]).update(status=ArticleStatus.FAILED)
    assert api.delete(f"/articles/{row['id']}").status_code == 204
    assert api.get("/types").json()["default"] == "ultimate_guide"


def test_viewer_cannot_write(client, workspace, synced):
    from conftest import make_user
    from workspaces.models import Role

    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    client.force_login(viewer)
    response = client.post(
        f"/api/v1/websites/{synced.pk}/blog/ideas/suggest",
        data={},
        content_type=JSON,
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )
    assert response.status_code == 403


# ---- all posts, score and AI fixes ----


def test_posts_list_shows_score_and_problems(api, post):
    data = api.get("/posts").json()
    assert data["weak_below"] == 70
    row = next(r for r in data["posts"] if r["id"] == str(post.pk))
    assert 0 <= row["score"] <= 100
    assert row["analysis_status"] == ""
    scores = [r["score"] for r in data["posts"]]
    assert scores == sorted(scores)


def test_improve_starts_one_review_and_fixes_apply_through_actions(
    api, post, monkeypatch, django_capture_on_commit_callbacks
):
    from content import review
    from content.models import PageSuggestion
    from content.tests.conftest import GOOD_REVIEW

    queued = []
    monkeypatch.setattr("content.blog.api_posts.analyze_page.delay", queued.append)
    assert api.post(f"/posts/{post.pk}/improve").json() == {"started": 1}
    assert api.post(f"/posts/{post.pk}/improve").json() == {"started": 0}
    assert len(queued) == 1
    analysis = post.analyses.get()
    script_llm(GOOD_REVIEW)
    review.run_analysis(analysis)
    row = next(r for r in api.get("/posts").json()["posts"] if r["id"] == str(post.pk))
    assert row["analysis_status"] == "done"
    assert row["ai_fixes"] == ["meta_description", "seo_title"]
    assert row["advice_count"] == 1
    applied = api.post(f"/posts/{post.pk}/apply-ai")
    assert applied.status_code == 200
    assert applied.json()["status"] in {"approved", "succeeded", "verified", "executing"}
    assert PageSuggestion.objects.filter(item=post, field="advice", status="proposed").count() == 1


def test_apply_ai_without_fixes_is_refused(api, post):
    assert api.post(f"/posts/{post.pk}/apply-ai").status_code == 422


def test_improve_weak_starts_only_weak_posts(api, post, monkeypatch):
    queued = []
    monkeypatch.setattr("content.blog.api_posts.analyze_page.delay", queued.append)
    assert api.post("/posts/improve-weak", {"below": 101}).json()["started"] >= 1
    assert api.post("/posts/improve-weak", {"below": 0}).json() == {"started": 0}


def test_an_unexpected_crash_marks_the_article_failed_instead_of_leaving_it_busy(
    synced, monkeypatch
):
    article = writer.start_article(
        synced, requested_by="u", title="t", primary_keyword="k", blog_type="how_to", min_words=800
    )

    def crash(_article):
        raise RuntimeError("boom")

    monkeypatch.setattr(writer, "_plan_step", crash)
    assert writer.advance(article) is False
    article.refresh_from_db()
    assert article.status == ArticleStatus.FAILED and article.error == "unexpected"


def test_library_tells_how_fresh_the_copy_is_and_reload_queues_one_sync(api, post, monkeypatch):
    queued = []
    monkeypatch.setattr("content.blog.api_posts.sync_website.delay", queued.append)
    first = api.get("/library").json()
    assert first["posts"] >= 1 and first["syncing"] is False
    assert first["synced_at"] is not None  # the fixture synced the site once
    assert api.post("/library/reload").json()["syncing"] is True
    assert api.post("/library/reload").json()["syncing"] is True
    assert len(queued) == 1  # a second press while one is queued does nothing
