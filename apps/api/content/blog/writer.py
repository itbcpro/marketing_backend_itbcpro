"""The article pipeline: plan, then one section per step, then FAQ and audit.

Each call to ``advance`` does exactly one step and saves, so a restart loses at most one section and
every step stays well inside a task time limit.
"""

import re
from typing import Any

from django.utils import timezone

from content import ai
from content.blog import context, gate, images, kinds, openai_images, weights
from content.blog.schemas import AuditOut, FaqOut, Plan, SectionOut
from content.models import Article, ArticleIdea, ArticleStatus, IdeaStatus, Website
from core.errors import DomainError, ValidationFailedError
from core.logging import get_logger
from llm import prompts

log = get_logger(__name__)

DEFAULT_MIN_WORDS = 8000
MIN_WORDS_FLOOR = 800
MIN_WORDS_CEILING = 20000
MAX_INSTRUCTION_CHARS = 20000
PLAN_HEADROOM = 1.1
SHORT_RETRY = 0.6
FAQ_TARGET = 20
PLAN_MAX_TOKENS = 32000
PLAN_EFFORT = "medium"
MIN_SECTION_WORDS = 150
SLUG = re.compile(r"[^a-z0-9]+")


def rules() -> str:
    return prompts.get_prompt("content/article-rules").body


def start_article(
    website: Website,
    *,
    requested_by: str,
    idea: ArticleIdea | None = None,
    title: str = "",
    primary_keyword: str = "",
    secondary_keywords: list[str] | None = None,
    blog_type: str = "",
    min_words: int = DEFAULT_MIN_WORDS,
    instruction: str = "",
) -> Article:
    """Create a queued article from an idea or from a topic typed by the owner."""
    chosen_type = blog_type or (idea.blog_type if idea else "")
    if not kinds.is_valid(chosen_type):
        raise ValidationFailedError("choose a blog type")
    title = (title or (idea.title if idea else "")).strip()
    keyword = (primary_keyword or (idea.primary_keyword if idea else "")).strip()
    if not title or not keyword:
        raise ValidationFailedError("a title and a primary keyword are required")
    if not MIN_WORDS_FLOOR <= min_words <= MIN_WORDS_CEILING:
        raise ValidationFailedError("the minimum length is out of range")
    article = Article.objects.create(
        workspace_id=website.workspace_id,
        website=website,
        idea=idea,
        title=title,
        primary_keyword=keyword,
        secondary_keywords=secondary_keywords or (idea.secondary_keywords if idea else []),
        blog_type=chosen_type,
        min_words=min_words,
        instruction=instruction[:MAX_INSTRUCTION_CHARS],
        step="plan",
        requested_by=requested_by,
    )
    if idea is not None:
        idea.status = IdeaStatus.WRITING
        idea.save(update_fields=["status", "updated_at"])
    return article


def retry(article: Article) -> Article:
    """Continue a failed article from the step that failed."""
    if article.status != ArticleStatus.FAILED:
        raise ValidationFailedError("only a failed article can be retried")
    article.status, article.error = ArticleStatus.QUEUED, ""
    article.save(update_fields=["status", "error", "updated_at"])
    return article


def progress(article: Article) -> int:
    """Rough percent done, for the progress bar."""
    total = len(article.plan.get("sections", [])) or 1
    if article.status == ArticleStatus.READY:
        return 100
    if article.step == "plan":
        return 3
    bonus = 10 if article.step in {"faq", "audit"} else 0
    return min(95, 5 + round(len(article.sections) / total * 80) + bonus)


def advance(article: Article) -> bool:
    """Do the next step. Returns True when more work remains."""
    if article.status in {ArticleStatus.READY, ArticleStatus.FAILED}:
        return False
    article.status = ArticleStatus.WRITING
    article.save(update_fields=["status", "updated_at"])
    try:
        _step(article)
    except DomainError as exc:
        log.warning(
            "article_step_failed", article=str(article.pk), step=article.step, code=exc.code.value
        )
        article.status, article.error = ArticleStatus.FAILED, exc.code.value
        article.save()
        return False
    except Exception:
        # Anything unexpected must not leave the article looking busy forever.
        log.exception("article_step_crashed", article=str(article.pk), step=article.step)
        article.status, article.error = ArticleStatus.FAILED, "unexpected"
        article.save()
        return False
    article.save()
    return article.status == ArticleStatus.WRITING


def _step(article: Article) -> None:
    if article.step == "plan":
        _plan_step(article)
    elif article.step.startswith("section:"):
        _section_step(article)
    elif article.step == "faq":
        _faq_step(article)
    elif article.step.startswith(("images:", "imgredo:")):
        _images_step(article)
    else:
        _audit_step(article)


def _plan_view(article: Article) -> dict[str, Any]:
    plan = article.plan
    return {
        "title": article.meta.get("h1", article.title),
        "primary_keyword": article.primary_keyword,
        "secondary_keywords": article.secondary_keywords,
        "blog_type": article.blog_type,
        "intent": plan.get("intent", ""),
        "audience": plan.get("audience", ""),
        "key_takeaways": plan.get("key_takeaways", []),
        "sections": [
            {"heading": s["heading"], "purpose": s["purpose"]} for s in plan.get("sections", [])
        ],
    }


def _summaries(article: Article) -> list[dict[str, str]]:
    return [{"heading": s["heading"], "summary": s["summary"]} for s in article.sections]


def _neighbors(article: Article) -> list[dict[str, str]]:
    topic = f"{article.title} {article.primary_keyword} {' '.join(article.secondary_keywords)}"
    return context.neighbors(article.website, topic)


def _plan_step(article: Article) -> None:
    website = article.website
    neighbors = _neighbors(article)
    idea = {
        "title": article.title,
        "primary_keyword": article.primary_keyword,
        "secondary_keywords": article.secondary_keywords,
        "blog_type": article.blog_type,
        "intent": article.idea.intent if article.idea else "",
        "angle": article.idea.angle if article.idea else "",
    }
    plan = ai.run_prompt(
        article.workspace_id,
        "content/article-outline",
        Plan,
        memory_query=f"article {article.title}",
        user_message="Plan the article.",
        max_tokens=PLAN_MAX_TOKENS,
        effort=PLAN_EFFORT,
        rules=rules(),
        type_guidance=kinds.guidance(article.blog_type),
        site=ai.dumps(context.site_view(website)),
        idea=ai.dumps(idea),
        neighbors=ai.dumps(neighbors),
        search=ai.dumps(context.search_evidence(website)),
        brief=ai.dumps(website.brief or {}),
        min_words=article.min_words,
        instruction=article.instruction or "(none)",
    )
    article.plan = _clean_plan(plan, {n["url"] for n in neighbors}, article.min_words)
    article.meta = {
        "h1": plan.h1.strip() or article.title,
        "seo_title": plan.seo_title.strip(),
        "meta_description": plan.meta_description.strip(),
        "slug": SLUG.sub("-", plan.slug.lower()).strip("-")[:80],
    }
    images.prepare(article, plan.model_dump()["image_plan"])
    article.step = "section:0"


def _clean_plan(plan: Plan, urls: set[str], min_words: int) -> dict[str, Any]:
    """Keep only real link targets and make the word targets add up to the minimum."""
    data = plan.model_dump()
    for section in data["sections"]:
        section["link_to"] = [u for u in section["link_to"] if u in urls]
        section["target_words"] = max(MIN_SECTION_WORDS, int(section["target_words"]))
    total = sum(s["target_words"] for s in data["sections"])
    goal = round(min_words * PLAN_HEADROOM)
    if total < goal:
        factor = goal / total
        for section in data["sections"]:
            section["target_words"] = round(section["target_words"] * factor)
    return data


def _section_step(article: Article) -> None:
    index = int(article.step.split(":")[1])
    sections = article.plan["sections"]
    if index >= len(sections):
        article.step = "faq"
        return
    spec = sections[index]
    out = _write_section(article, index, spec)
    article.sections = [*article.sections, _stored(out, spec)]
    article.step = f"section:{index + 1}" if index + 1 < len(sections) else "faq"


def _position(index: int, count: int) -> str:
    where = f"Section {index + 1} of {count}"
    if index == 0:
        return f"{where}; this is the introduction."
    if index + 1 == count:
        return f"{where}; this is the last section: close with the next step."
    return f"{where}."


def _write_section(article: Article, index: int, spec: dict[str, Any]) -> SectionOut:
    titles = {n["url"]: n["title"] for n in _neighbors(article)}
    links = [{"address": u, "page_title": titles[u]} for u in spec["link_to"] if u in titles]
    position = _position(index, len(article.plan["sections"]))
    extra = article.instruction or "(none)"
    out = _ask_section(article, spec, position, links, extra)
    if gate.count_words(out.markdown) < SHORT_RETRY * spec["target_words"]:
        extra += (
            " The previous attempt was too short. "
            f"Write at least {spec['target_words']} words of real substance."
        )
        out = _ask_section(article, spec, position, links, extra)
    return out


def _ask_section(
    article: Article, spec: dict[str, Any], position: str, links: list[dict[str, str]], extra: str
) -> SectionOut:
    return ai.run_prompt(
        article.workspace_id,
        "content/article-section",
        SectionOut,
        memory_query=f"article {article.title} {spec['heading']}",
        user_message="Write the section.",
        rules=rules(),
        type_guidance=kinds.guidance(article.blog_type),
        plan=ai.dumps(_plan_view(article)),
        section=ai.dumps(spec),
        written=ai.dumps(_summaries(article)),
        links=ai.dumps(links),
        position=position,
        instruction=extra,
    )


def _stored(out: SectionOut, spec: dict[str, Any]) -> dict[str, Any]:
    markdown = out.markdown.strip()
    if not markdown.startswith("## "):
        markdown = f"## {spec['heading']}\n\n{markdown}"
    return {
        "heading": spec["heading"],
        "markdown": markdown,
        "summary": out.summary,
        "open_claims": out.open_claims,
        "words": gate.count_words(markdown),
        "target": spec["target_words"],
    }


def _faq_step(article: Article) -> None:
    faq = ai.run_prompt(
        article.workspace_id,
        "content/article-faq",
        FaqOut,
        memory_query=f"faq {article.title}",
        user_message="Write the FAQ.",
        rules=rules(),
        plan=ai.dumps(_plan_view(article)),
        questions=ai.dumps(article.plan.get("faq_questions", [])),
        written=ai.dumps(_summaries(article)),
        count=FAQ_TARGET,
        instruction=article.instruction or "(none)",
    )
    article.faq = [i.model_dump() for i in faq.items]
    article.step = "images:0" if images.wanted(article) else "audit"


def _audit_step(article: Article) -> None:
    neighbors = _neighbors(article)
    measured = gate.run(article, {n["url"] for n in neighbors}, context.domain_of(article.website))
    audit = ai.run_prompt(
        article.workspace_id,
        "content/article-audit",
        AuditOut,
        memory_query=f"article audit {article.title}",
        user_message="Audit the article.",
        rules=rules(),
        plan=ai.dumps(_plan_view(article)),
        article=gate.full_text(article),
        gate=ai.dumps(measured),
        neighbors=ai.dumps(neighbors),
    )
    article.gate = measured
    article.audit = audit.model_dump()
    article.score = weights.total(audit.scores, measured["failed"])
    article.word_count = measured["word_count"]
    article.step, article.status = "done", ArticleStatus.READY
    article.finished_at = timezone.now()
    if article.idea is not None:
        article.idea.status = IdeaStatus.WRITTEN
        article.idea.save(update_fields=["status", "updated_at"])


def _images_step(article: Article) -> None:
    """Draw one image per call. ``imgredo`` only draws the ones queued again, then finishes."""
    kind, _, raw = article.step.partition(":")
    index = int(raw)
    redo = kind == "imgredo"
    total = len(article.images)
    wanted = "queued" if redo else "planned"
    while index < total and article.images[index]["status"] != wanted:
        index += 1
    if index < total:
        error = images.make_one(article, index)
        article.step = f"{kind}:{index + 1}"
        if error == openai_images.NO_CREDITS:
            images.fail_rest(article, index + 1, error)  # do not ask again for every image
        return
    if redo:
        article.step, article.status = "done", ArticleStatus.READY
    else:
        article.step = "audit"


def redraw(article: Article, image_ids: list[str]) -> Article:
    """Queue some images (or all that failed or were skipped) to be drawn again."""
    if article.status != ArticleStatus.READY:
        raise ValidationFailedError("wait until the article has finished writing")
    if not openai_images.has_key(article.workspace_id):
        raise ValidationFailedError("connect an OpenAI API key to generate images")
    if not article.images and article.plan.get("image_plan"):
        images.prepare(
            article, article.plan["image_plan"]
        )  # articles planned before images existed
    chosen = 0
    for entry in article.images:
        if entry["id"] in image_ids or (not image_ids and entry["status"] != "ready"):
            entry["status"], chosen = "queued", chosen + 1
    if not chosen:
        raise ValidationFailedError("there are no images to draw")
    article.step, article.status = "imgredo:0", ArticleStatus.QUEUED
    article.save()
    return article
