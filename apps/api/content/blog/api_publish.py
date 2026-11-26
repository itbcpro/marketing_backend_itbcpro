"""Publishing and images of a finished article."""

import uuid
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from actions import services as action_services
from actions.models import ActionStatus, ProposedAction
from content import seo_meta
from content.blog import images, openai_images, publish, writer
from content.blog.api import ArticleRow, _article, _row
from content.tasks import write_article
from content.wordpress import factory
from core.errors import DomainError, NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from media.models import MediaAsset
from workspaces.permissions import Permission

router = Router(tags=["blog"], auth=api_auth)

LIVE_KINDS = (publish.KIND, publish.STATUS_KIND, publish.UPDATE_KIND)


class PublishInfo(Schema):
    connected: bool
    reason: str
    elementor: bool
    elementor_version: str
    seo_fields: bool
    images_ready: int
    images_total: int
    image_provider: bool
    snippet: str
    admin_url: str
    status: str


class PublishIn(Schema):
    status: str = "draft"
    schedule_at: str = ""
    elementor: bool = True


class Applied(Schema):
    action_id: uuid.UUID
    status: str


class RedrawIn(Schema):
    image_ids: list[str] = []


class PromptOut(Schema):
    prompt: str


class AttachIn(Schema):
    asset_id: uuid.UUID


def _readiness(article: Any) -> tuple[bool, str, bool, str, bool]:
    """Ask the site what it allows: ``(connected, reason, elementor, version, seo_fields)``."""
    if article.website.connection is None:
        return False, "no_connection", False, "", False
    client = factory.for_connection(article.website.connection)
    try:
        exposed = client.exposed_meta_keys("posts")
        version = client.elementor_version()
    except DomainError as exc:
        return False, str(exc)[:120] or "unreachable", False, "", False
    keys = seo_meta.seo_keys(article.website)
    return (
        True,
        "",
        all(key in exposed for key in publish.ELEMENTOR_KEYS),
        version,
        bool(keys) and all(key in exposed for key in keys.values()),
    )


@router.get(
    "/{website_id}/blog/articles/{article_id}/publish-info",
    response=PublishInfo,
    operation_id="blog_article_publish_info",
)
def publish_info(
    request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID
) -> dict[str, Any]:
    article = _article(request, website_id, article_id, Permission.VIEW)
    connected, reason, elementor, version, seo_fields = _readiness(article)
    base = article.website.base_url.rstrip("/")
    return {
        "connected": connected,
        "reason": reason,
        "elementor": elementor,
        "elementor_version": version,
        "seo_fields": seo_fields,
        "images_ready": images.ready_count(article),
        "images_total": len(article.images),
        "image_provider": openai_images.has_key(article.workspace_id),
        "snippet": "" if elementor or not connected else seo_meta.ELEMENTOR_SNIPPET,
        "admin_url": (
            f"{base}/wp-admin/post.php?post={article.wp_post_id}&action=elementor"
            if article.wp_post_id
            else ""
        ),
        "status": article.wp_status,
    }


def _propose(
    request: HttpRequest, article: Any, kind: str, payload: dict[str, Any], summary: str
) -> Applied:
    user: User = request.auth  # type: ignore[attr-defined]
    action = action_services.propose(
        workspace_id=article.workspace_id,
        kind=kind,
        payload=payload,
        requested_by=f"user:{user.pk}",
        summary=summary[:300],
        # Every press is a new request: a repeat after a failure must not return the failed action.
        # Double presses are stopped by ``_busy`` instead.
        idempotency_key=f"blog:{article.pk}:{uuid.uuid4()}",
    )
    if action.status == ActionStatus.AWAITING_APPROVAL:
        action = action_services.approve(
            workspace_id=article.workspace_id, action_id=action.pk, user=user
        )
    return Applied(action_id=action.pk, status=action.status)


def _busy(article: Any) -> bool:
    return ProposedAction.objects.filter(
        workspace_id=article.workspace_id,
        kind__in=LIVE_KINDS,
        payload__article_id=str(article.pk),
        status__in=[ActionStatus.AWAITING_APPROVAL, ActionStatus.APPROVED, ActionStatus.EXECUTING],
    ).exists()


@router.post(
    "/{website_id}/blog/articles/{article_id}/publish",
    response=Applied,
    operation_id="blog_article_publish",
)
def publish_article(
    request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID, payload: PublishIn
) -> Applied:
    article = _article(request, website_id, article_id, Permission.EDIT_CONTENT)
    if article.status != "ready":
        raise ValidationFailedError("wait until the article has finished writing")
    if article.wp_status == publish.LIVE:
        raise ValidationFailedError("this post is already live; edit it in WordPress")
    if payload.status != "draft" and not article.wp_post_id:
        raise ValidationFailedError("send the article to WordPress as a draft first")
    if _busy(article):
        raise ValidationFailedError("another change to this post is still in progress")
    try:
        spec = publish.PublishArticle(
            website_id=website_id,
            article_id=article.pk,
            status=payload.status,  # type: ignore[arg-type]
            schedule_at=payload.schedule_at,
            elementor=payload.elementor,
        )
    except ValueError as exc:
        raise ValidationFailedError(str(exc)[:200]) from exc
    verb = {"draft": "Send", "publish": "Publish", "future": "Schedule"}[payload.status]
    return _propose(
        request,
        article,
        publish.KIND,
        spec.model_dump(mode="json"),
        f"{verb} article: {article.title}",
    )


@router.post(
    "/{website_id}/blog/articles/{article_id}/trash",
    response=Applied,
    operation_id="blog_article_trash",
)
def trash_article(request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID) -> Applied:
    article = _article(request, website_id, article_id, Permission.EDIT_CONTENT)
    if not article.wp_post_id or article.wp_status == publish.LIVE:
        raise ValidationFailedError("only a draft or scheduled post can be moved to the trash here")
    if _busy(article):
        raise ValidationFailedError("another change to this post is still in progress")
    spec = publish.SetPostStatus(website_id=website_id, article_id=article.pk, status="trash")
    return _propose(
        request,
        article,
        publish.STATUS_KIND,
        spec.model_dump(mode="json"),
        f"Trash: {article.title}",
    )


@router.post(
    "/{website_id}/blog/articles/{article_id}/images/redraw",
    response=ArticleRow,
    operation_id="blog_article_images_redraw",
)
def redraw_images(
    request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID, payload: RedrawIn
) -> dict[str, Any]:
    article = _article(request, website_id, article_id, Permission.EDIT_CONTENT)
    writer.redraw(article, payload.image_ids)
    write_article.delay(str(article.pk))
    return _row(article)


def _settled(request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID) -> Any:
    article = _article(request, website_id, article_id, Permission.EDIT_CONTENT)
    if article.status != "ready":
        raise ValidationFailedError("wait until the article has finished writing")
    return article


@router.get(
    "/{website_id}/blog/articles/{article_id}/images/{image_id}/prompt",
    response=PromptOut,
    operation_id="blog_article_image_prompt",
)
def image_prompt(
    request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID, image_id: str
) -> PromptOut:
    """The instructions used (or that would be used) to draw one image, to use in another tool."""
    article = _settled(request, website_id, article_id)
    return PromptOut(prompt=images.prompt_for(article, image_id))


@router.post(
    "/{website_id}/blog/articles/{article_id}/images/{image_id}/attach",
    response=ArticleRow,
    operation_id="blog_article_image_attach",
)
def attach_image(
    request: HttpRequest,
    website_id: uuid.UUID,
    article_id: uuid.UUID,
    image_id: str,
    payload: AttachIn,
) -> dict[str, Any]:
    """Use a picture already uploaded to the media library for this image slot."""
    article = _settled(request, website_id, article_id)
    asset = MediaAsset.objects.filter(
        pk=payload.asset_id, workspace_id=article.workspace_id
    ).first()
    if asset is None:
        raise NotFoundError("picture not found")
    images.attach_upload(article, image_id, asset)
    return _row(article)


@router.post(
    "/{website_id}/blog/articles/{article_id}/update-post",
    response=Applied,
    operation_id="blog_article_update_post",
)
def update_post(request: HttpRequest, website_id: uuid.UUID, article_id: uuid.UUID) -> Applied:
    """Send the current text and pictures to the post already on the site, live or not."""
    article = _settled(request, website_id, article_id)
    if not article.wp_post_id:
        raise ValidationFailedError("this article is not on the site yet")
    if _busy(article):
        raise ValidationFailedError("another change to this post is still in progress")
    spec = publish.UpdatePost(website_id=website_id, article_id=article.pk)
    return _propose(
        request,
        article,
        publish.UPDATE_KIND,
        spec.model_dump(mode="json"),
        f"Update post: {article.title}",
    )
