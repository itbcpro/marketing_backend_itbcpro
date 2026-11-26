"""Send a finished article to WordPress as a draft (Elementor layout when the site allows it).

Publishing is an R2 action: it is proposed, approved by a person, executed by a worker, verified
and reversible. A post is only ever created as a draft first; going live is a second, separate
action, and a post that is already live is never rewritten from here.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from actions.models import ProposedAction, RiskClass
from actions.registry import ActionKind, register_action_kind
from content import seo_meta
from content.blog import images, render
from content.models import Article, Website
from content.wordpress import factory
from content.wordpress.client import WordPressClient
from core.errors import NotFoundError, PermanentProviderError
from media.models import MediaAsset
from media.storage import get_storage

KIND = "content.publish_article"
STATUS_KIND = "content.set_post_status"
UPDATE_KIND = "content.update_post"
ELEMENTOR_KEYS = ("_elementor_data", "_elementor_edit_mode", "_elementor_template_type")
POSTS = "posts"
LIVE = "publish"


class PublishArticle(BaseModel):
    website_id: uuid.UUID
    article_id: uuid.UUID
    status: Literal["draft", "publish", "future"] = "draft"
    schedule_at: str = ""
    """UTC ISO time, required for ``future``."""
    elementor: bool = True

    @field_validator("schedule_at")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        if value:
            datetime.fromisoformat(value)
        return value

    def model_post_init(self, __context: Any) -> None:
        if self.status == "future" and not self.schedule_at:
            raise ValueError("choose a date and time to schedule the post")


class UpdatePost(BaseModel):
    """Rewrite the text and pictures of a post already on the site; its status is not touched."""

    website_id: uuid.UUID
    article_id: uuid.UUID
    elementor: bool = True


class SetPostStatus(BaseModel):
    website_id: uuid.UUID
    article_id: uuid.UUID
    status: Literal["draft", "trash"]


def _article(action: ProposedAction, website_id: uuid.UUID, article_id: uuid.UUID) -> Article:
    article = (
        Article.objects.select_related("website__connection")
        .filter(pk=article_id, website_id=website_id, workspace_id=action.workspace_id)
        .first()
    )
    if article is None or article.website.connection is None:
        raise NotFoundError("this article cannot be sent to WordPress")
    return article


def _client(site: Website) -> WordPressClient:
    return factory.for_connection(site.connection)  # type: ignore[arg-type]


def upload_images(article: Article, client: WordPressClient) -> int:
    """Upload every drawn image that is not in the media library yet. Progress is saved."""
    uploaded = 0
    for entry in article.images:
        if entry["status"] != "ready" or entry.get("wp_media_id"):
            continue
        asset = MediaAsset.objects.filter(pk=entry["asset_id"]).first()
        if asset is None:
            continue
        with get_storage().open(asset.storage_key) as handle:
            data = images.compress(handle.read())  # large files are refused by some hosts
        saved = client.upload_media(entry["filename"], "image/webp", data)
        client.update_media(
            int(saved["id"]),
            {
                "alt_text": entry["alt"],
                "caption": entry.get("caption", ""),
                "title": entry["filename"].rsplit(".", 1)[0].replace("-", " "),
            },
        )
        entry["wp_media_id"], entry["wp_url"] = int(saved["id"]), str(saved.get("source_url", ""))
        article.save(update_fields=["images", "updated_at"])
        uploaded += 1
    return uploaded


def _meta(
    article: Article, client: WordPressClient, payload: PublishArticle | UpdatePost
) -> tuple[dict[str, str], dict[str, bool]]:
    """Post meta to write and a report of what the site allowed."""
    site = article.website
    exposed = client.exposed_meta_keys(POSTS)
    meta: dict[str, str] = {}
    report = {"elementor": False, "seo_fields": False}
    if payload.elementor and all(key in exposed for key in ELEMENTOR_KEYS):
        meta.update(
            _elementor_data=json.dumps(render.build_elementor(article), ensure_ascii=False),
            _elementor_edit_mode="builder",
            _elementor_template_type="wp-post",
        )
        version = client.elementor_version()
        if version and "_elementor_version" in exposed:
            meta["_elementor_version"] = version
        report["elementor"] = True
    keys = seo_meta.seo_keys(site)
    if keys and all(key in exposed for key in keys.values()):
        meta[keys["seo_title"]] = str(article.meta.get("seo_title", ""))
        meta[keys["meta_description"]] = str(article.meta.get("meta_description", ""))
        report["seo_fields"] = True
    return meta, report


def _body(article: Article, payload: PublishArticle, meta: dict[str, str]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": article.meta.get("h1") or article.title,
        "slug": article.meta.get("slug", ""),
        "status": payload.status,
        "content": render.build_html(article),
        "excerpt": article.meta.get("meta_description", ""),
        "meta": meta,
    }
    featured = render.featured_image(article)
    if featured:
        body["featured_media"] = featured["wp_media_id"]
    if payload.status == "future":
        body["date_gmt"] = payload.schedule_at
    return body


def execute(action: ProposedAction) -> dict[str, Any]:
    payload = PublishArticle.model_validate(action.effective_payload)
    article = _article(action, payload.website_id, payload.article_id)
    client = _client(article.website)
    previous = ""
    if article.wp_post_id:
        previous = str(client.item(POSTS, article.wp_post_id).get("status", ""))
        if previous == LIVE:
            raise PermanentProviderError("post_already_live", provider="wordpress")
    uploaded = upload_images(article, client)
    meta, report = _meta(article, client, payload)
    body = _body(article, payload, meta)
    if article.wp_post_id:
        saved = client.update_item(POSTS, article.wp_post_id, body)
    else:
        saved = client.create_item(POSTS, body)
        article.wp_post_id = int(saved["id"])
        article.save(update_fields=["wp_post_id", "updated_at"])
    article.wp_url = str(saved.get("link", ""))
    article.wp_status = str(saved.get("status", payload.status))
    article.save(update_fields=["wp_url", "wp_status", "updated_at"])
    return {
        "post_id": article.wp_post_id,
        "link": article.wp_url,
        "status": article.wp_status,
        "created": not previous,
        "previous_status": previous,
        "uploaded_images": uploaded,
        **report,
    }


def verify(action: ProposedAction) -> bool:
    payload = PublishArticle.model_validate(action.effective_payload)
    article = _article(action, payload.website_id, payload.article_id)
    if not article.wp_post_id:
        return False
    live = _client(article.website).item(POSTS, article.wp_post_id)
    return str(live.get("status", "")) == payload.status


def preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = PublishArticle.model_validate(action.payload)
    article = Article.objects.filter(
        pk=payload.article_id, workspace_id=action.workspace_id
    ).first()
    if article is None:
        return None, {"status": payload.status}
    return (
        {"status": article.wp_status or "not on the site"},
        {
            "title": article.meta.get("h1") or article.title,
            "status": payload.status,
            "words": article.word_count,
            "images": sum(1 for e in article.images if e["status"] == "ready"),
        },
    )


def rollback_payload(action: ProposedAction) -> dict[str, Any]:
    payload = PublishArticle.model_validate(action.effective_payload)
    created = bool((action.result or {}).get("created"))
    return {
        "website_id": str(payload.website_id),
        "article_id": str(payload.article_id),
        "status": "trash" if created else "draft",
    }


def execute_update(action: ProposedAction) -> dict[str, Any]:
    """Send the current text and pictures to a post that is already on the site (even live)."""
    payload = UpdatePost.model_validate(action.effective_payload)
    article = _article(action, payload.website_id, payload.article_id)
    if not article.wp_post_id:
        raise PermanentProviderError("post_not_on_site", provider="wordpress")
    client = _client(article.website)
    uploaded = upload_images(article, client)
    meta, report = _meta(article, client, payload)
    shape = PublishArticle(
        website_id=payload.website_id,
        article_id=payload.article_id,
        status="publish",
        elementor=payload.elementor,
    )
    body = _body(article, shape, meta)
    body.pop("status")  # an update never changes whether the post is live
    saved = client.update_item(POSTS, article.wp_post_id, body)
    article.wp_url = str(saved.get("link", article.wp_url))
    article.wp_status = str(saved.get("status", article.wp_status))
    article.save(update_fields=["wp_url", "wp_status", "updated_at"])
    return {
        "post_id": article.wp_post_id,
        "link": article.wp_url,
        "status": article.wp_status,
        "uploaded_images": uploaded,
        **report,
    }


def execute_status(action: ProposedAction) -> dict[str, Any]:
    payload = SetPostStatus.model_validate(action.effective_payload)
    article = _article(action, payload.website_id, payload.article_id)
    if not article.wp_post_id:
        raise PermanentProviderError("post_not_on_site", provider="wordpress")
    client = _client(article.website)
    before = str(client.item(POSTS, article.wp_post_id).get("status", ""))
    if payload.status == "trash":
        client.trash_item(POSTS, article.wp_post_id)
        article.wp_post_id, article.wp_status, article.wp_url = None, "trash", ""
    else:
        saved = client.update_item(POSTS, article.wp_post_id, {"status": payload.status})
        article.wp_status = str(saved.get("status", payload.status))
    article.save(update_fields=["wp_post_id", "wp_status", "wp_url", "updated_at"])
    return {"before": before, "after": payload.status}


def register_action_kinds() -> None:
    register_action_kind(
        ActionKind(
            kind=KIND,
            payload_model=PublishArticle,
            risk=RiskClass.R2,
            executor=execute,
            description="Send a blog article to WordPress as a draft, or publish or schedule it.",
            preview=preview,
            verify=verify,
            rollback_kind=STATUS_KIND,
            rollback_payload=rollback_payload,
        )
    )
    register_action_kind(
        ActionKind(
            kind=UPDATE_KIND,
            payload_model=UpdatePost,
            risk=RiskClass.R2,
            executor=execute_update,
            description="Update the text and pictures of a blog post that is already on the site.",
        )
    )
    register_action_kind(
        ActionKind(
            kind=STATUS_KIND,
            payload_model=SetPostStatus,
            risk=RiskClass.R2,
            executor=execute_status,
            description="Move a blog post back to draft or to the WordPress trash.",
        )
    )
