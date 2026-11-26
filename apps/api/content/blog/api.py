"""Blog studio: topic ideas, article writing progress, finished drafts."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from actions.models import ProposedAction
from content.blog import context, gate, kinds, openai_images, topics, writer
from content.models import Article, ArticleIdea, ArticleStatus, IdeaStatus, Website
from content.tasks import write_article
from core.errors import NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["blog"], auth=api_auth)

BUSY = {ArticleStatus.QUEUED, ArticleStatus.WRITING}


class IdeaOut(Schema):
    id: uuid.UUID
    title: str
    primary_keyword: str
    secondary_keywords: list[str]
    intent: str
    blog_type: str
    angle: str
    why_now: str
    evidence: str
    cluster: str
    priority: str
    overlaps: list[str]
    status: str
    created_at: datetime


class SuggestIn(Schema):
    focus: str = ""
    count: int = topics.DEFAULT_COUNT
    blog_type: str = ""


class IdeaPatchIn(Schema):
    blog_type: str


class ArticleIn(Schema):
    idea_id: uuid.UUID | None = None
    title: str = ""
    primary_keyword: str = ""
    secondary_keywords: list[str] = []
    blog_type: str = ""
    min_words: int = writer.DEFAULT_MIN_WORDS
    instruction: str = ""


class ArticleRow(Schema):
    id: uuid.UUID
    title: str
    primary_keyword: str
    blog_type: str
    status: str
    step: str
    progress: int
    word_count: int
    score: int | None
    error: str
    created_at: datetime
    finished_at: datetime | None


class ImageOut(Schema):
    id: str
    placement: str
    concept: str
    alt: str
    caption: str
    prompt: str = ""
    status: str
    filename: str
    asset_id: str
    error: str
    wp_media_id: int | None
    wp_url: str


class LinkOut(Schema):
    anchor: str
    url: str
    section: str
    internal: bool


class PublishStateOut(Schema):
    id: str
    kind: str
    status: str
    error: str
    summary: str
    created_at: str
    result: dict[str, Any]


class ArticleDetail(ArticleRow):
    secondary_keywords: list[str]
    min_words: int
    meta: dict[str, Any]
    plan: dict[str, Any]
    sections: list[dict[str, Any]]
    faq: list[dict[str, Any]]
    audit: dict[str, Any]
    gate: dict[str, Any]
    images: list[ImageOut]
    with_images: bool
    wp_post_id: int | None
    wp_url: str
    wp_status: str
    publish_state: PublishStateOut | None
    links: list[LinkOut]
    image_provider: bool


class ArticleStatusOut(Schema):
    """What changes while an article is written or published; small enough to poll."""

    status: str
    step: str
    progress: int
    wp_status: str
    images_ready: int
    publish_status: str
    signature: str


class TypesOut(Schema):
    types: list[str]
    default: str
    min_words: int


def _website(request: HttpRequest, website_id: uuid.UUID, permission: Permission) -> Website:
    membership = resolve_workspace(request, permission)
    site = Website.objects.filter(pk=website_id, workspace_id=membership.workspace_id).first()
    if site is None:
        raise NotFoundError("website not found")
    return site


def _actor(request: HttpRequest) -> str:
    user: User = request.auth  # type: ignore[attr-defined]
    return f"user:{user.pk}"


def _row(article: Article) -> dict[str, Any]:
    return {
        "id": article.pk,
        "title": article.title,
        "primary_keyword": article.primary_keyword,
        "blog_type": article.blog_type,
        "status": article.status,
        "step": article.step,
        "progress": writer.progress(article),
        "word_count": article.word_count,
        "score": article.score,
        "error": article.error,
        "created_at": article.created_at,
        "finished_at": article.finished_at,
    }


def article_links(article: Article) -> list[dict[str, Any]]:
    """Every link in the article text, its section, and whether it stays on the site."""
    domain = context.domain_of(article.website)
    found: list[dict[str, Any]] = []
    for section in article.sections:
        for anchor, url in gate.MD_LINK.findall(section["markdown"]):
            found.append(
                {
                    "anchor": anchor,
                    "url": url,
                    "section": section["heading"],
                    "internal": bool(domain) and domain in url,
                }
            )
    return found


def _publish_state(article: Article) -> dict[str, Any] | None:
    """The latest publish or status change of this article, with its outcome."""
    action = (
        ProposedAction.objects.filter(
            workspace_id=article.workspace_id,
            kind__in=[
                "content.publish_article",
                "content.set_post_status",
                "content.update_post",
            ],
            payload__article_id=str(article.pk),
        )
        .order_by("-created_at")
        .first()
    )
    if action is None:
        return None
    return {
        "id": str(action.pk),
        "kind": action.kind,
        "status": action.status,
        "error": action.error,
        "summary": action.summary,
        "created_at": action.created_at.isoformat(),
        "result": action.result or {},
    }


def _detail(article: Article) -> dict[str, Any]:
    return {
        **_row(article),
        "secondary_keywords": article.secondary_keywords,
        "min_words": article.min_words,
        "meta": article.meta,
        "plan": article.plan,
        "sections": article.sections,
        "faq": article.faq,
        "audit": article.audit,
        "gate": article.gate,
        "images": article.images,
        "with_images": article.with_images,
        "wp_post_id": article.wp_post_id,
        "wp_url": article.wp_url,
        "wp_status": article.wp_status,
        "publish_state": _publish_state(article),
        "links": article_links(article),
        "image_provider": openai_images.has_key(article.workspace_id),
    }


def _article(
    request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID, permission: Permission
) -> Article:
    site = _website(request, website_id, permission)
    article = Article.objects.select_related("idea").filter(pk=article_id, website=site).first()
    if article is None:
        raise NotFoundError("article not found")
    return article


def _idea(site: Website, idea_id: uuid.UUID) -> ArticleIdea:
    idea = site.ideas.filter(pk=idea_id).first()
    if idea is None:
        raise NotFoundError("idea not found")
    return idea


@router.get("/{website_id}/blog/types", response=TypesOut, operation_id="blog_types")
def blog_types(request: HttpRequest, website_id: uuid.UUID) -> dict[str, Any]:
    _website(request, website_id, Permission.VIEW)
    return {
        "types": list(kinds.BLOG_TYPES),
        "default": kinds.DEFAULT_TYPE,
        "min_words": writer.DEFAULT_MIN_WORDS,
    }


@router.get("/{website_id}/blog/ideas", response=list[IdeaOut], operation_id="blog_ideas")
def list_ideas(request: HttpRequest, website_id: uuid.UUID) -> list[ArticleIdea]:
    site = _website(request, website_id, Permission.VIEW)
    return list(site.ideas.exclude(status=IdeaStatus.DISMISSED)[:100])


@router.post(
    "/{website_id}/blog/ideas/suggest", response=list[IdeaOut], operation_id="blog_ideas_suggest"
)
def suggest_ideas(
    request: HttpRequest, website_id: uuid.UUID, payload: SuggestIn
) -> list[ArticleIdea]:
    site = _website(request, website_id, Permission.EDIT_CONTENT)
    return topics.suggest(
        site,
        requested_by=_actor(request),
        focus=payload.focus,
        count=payload.count,
        blog_type=payload.blog_type,
    )


@router.put("/{website_id}/blog/ideas/{idea_id}", response=IdeaOut, operation_id="blog_idea_patch")
def patch_idea(
    request: HttpRequest, website_id: uuid.UUID, idea_id: uuid.UUID, payload: IdeaPatchIn
) -> ArticleIdea:
    idea = _idea(_website(request, website_id, Permission.EDIT_CONTENT), idea_id)
    if not kinds.is_valid(payload.blog_type):
        raise ValidationFailedError("unknown blog type")
    idea.blog_type = payload.blog_type
    idea.save(update_fields=["blog_type", "updated_at"])
    return idea


@router.post(
    "/{website_id}/blog/ideas/{idea_id}/dismiss", response=IdeaOut, operation_id="blog_idea_dismiss"
)
def dismiss_idea(request: HttpRequest, website_id: uuid.UUID, idea_id: uuid.UUID) -> ArticleIdea:
    idea = _idea(_website(request, website_id, Permission.EDIT_CONTENT), idea_id)
    idea.status = IdeaStatus.DISMISSED
    idea.save(update_fields=["status", "updated_at"])
    return idea


@router.get("/{website_id}/blog/articles", response=list[ArticleRow], operation_id="blog_articles")
def list_articles(request: HttpRequest, website_id: uuid.UUID) -> list[dict[str, Any]]:
    site = _website(request, website_id, Permission.VIEW)
    return [_row(a) for a in site.articles.all()[:100]]


@router.post("/{website_id}/blog/articles", response=ArticleRow, operation_id="blog_article_create")
def create_article(
    request: HttpRequest, website_id: uuid.UUID, payload: ArticleIn
) -> dict[str, Any]:
    site = _website(request, website_id, Permission.EDIT_CONTENT)
    idea = _idea(site, payload.idea_id) if payload.idea_id is not None else None
    if idea is not None and idea.status == IdeaStatus.WRITING:
        raise ValidationFailedError("this idea is already being written")
    article = writer.start_article(
        site,
        requested_by=_actor(request),
        idea=idea,
        title=payload.title,
        primary_keyword=payload.primary_keyword,
        secondary_keywords=payload.secondary_keywords,
        blog_type=payload.blog_type,
        min_words=payload.min_words,
        instruction=payload.instruction,
    )
    write_article.delay(str(article.pk))
    return _row(article)


@router.get(
    "/{website_id}/blog/articles/{article_id}",
    response=ArticleDetail,
    operation_id="blog_article_detail",
)
def article_detail(
    request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID
) -> dict[str, Any]:
    return _detail(_article(request, website_id, article_id, Permission.VIEW))


@router.get(
    "/{website_id}/blog/articles/{article_id}/status",
    response=ArticleStatusOut,
    operation_id="blog_article_status",
)
def article_status(
    request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID
) -> dict[str, Any]:
    article = _article(request, website_id, article_id, Permission.VIEW)
    state = _publish_state(article)
    publish_status = state["status"] if state else ""
    ready = sum(1 for e in article.images if e["status"] == "ready")
    return {
        "status": article.status,
        "step": article.step,
        "progress": writer.progress(article),
        "wp_status": article.wp_status,
        "images_ready": ready,
        "publish_status": publish_status,
        "signature": "|".join(
            [article.status, article.step, article.wp_status, str(ready), publish_status]
        ),
    }


@router.post(
    "/{website_id}/blog/articles/{article_id}/retry",
    response=ArticleRow,
    operation_id="blog_article_retry",
)
def retry_article(
    request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID
) -> dict[str, Any]:
    article = writer.retry(_article(request, website_id, article_id, Permission.EDIT_CONTENT))
    write_article.delay(str(article.pk))
    return _row(article)


@router.delete(
    "/{website_id}/blog/articles/{article_id}",
    response={204: None},
    operation_id="blog_article_delete",
)
def delete_article(
    request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID
) -> Status[None]:
    article = _article(request, website_id, article_id, Permission.EDIT_CONTENT)
    if article.status in BUSY:
        raise ValidationFailedError("wait until the article has finished writing")
    if article.idea is not None and article.idea.status == IdeaStatus.WRITTEN:
        article.idea.status = IdeaStatus.SUGGESTED
        article.idea.save(update_fields=["status", "updated_at"])
    article.delete()
    return Status(204, None)
