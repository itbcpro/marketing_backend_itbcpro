"""Article images: plan, draw with OpenAI, store in the media library."""

import io
import re
from typing import Any

from PIL import Image

from content import ai
from content.blog import openai_images
from content.blog.schemas import ImagePrompt
from content.models import Article
from core.errors import DomainError, NotFoundError, ValidationFailedError
from core.logging import get_logger
from media import services as media
from media.models import MediaAsset, MediaKind, MediaSource, Rights

log = get_logger(__name__)

MAX_IMAGES = 6
MAX_WIDTH = 1536
WEBP_QUALITY = 82
FEATURED = "featured"
SAFE_NAME = re.compile(r"[^a-z0-9]+")
SHAPE_NOTE = {
    "landscape": "Landscape, about 3:2. Keep the subject away from the edges.",
}


def prepare(article: Article, planned: list[dict[str, Any]]) -> None:
    """Turn the editor's image plan into the article's image list (featured image first)."""
    ordered = sorted(planned, key=lambda p: p["placement"].lower() != FEATURED)[:MAX_IMAGES]
    article.images = [
        {
            "id": f"img{i + 1}",
            "placement": p["placement"],
            "concept": p["concept"],
            "alt": p["alt"],
            "caption": p["caption"],
            "prompt": "",
            "status": "planned",
            "filename": "",
            "asset_id": "",
            "error": "",
            "wp_media_id": None,
            "wp_url": "",
        }
        for i, p in enumerate(ordered)
    ]


def wanted(article: Article) -> bool:
    return (
        article.with_images and bool(article.images) and openai_images.has_key(article.workspace_id)
    )


def _article_view(article: Article, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": article.meta.get("h1", article.title),
        "primary_keyword": article.primary_keyword,
        "audience": article.plan.get("audience", ""),
        "section": entry["placement"],
    }


def _done_style(article: Article, index: int) -> list[dict[str, str]]:
    return [
        {"concept": e["concept"], "filename": e["filename"]}
        for i, e in enumerate(article.images)
        if i != index and e["status"] == "ready"
    ]


def _ask(article: Article, entry: dict[str, Any], index: int) -> ImagePrompt:
    return ai.run_prompt(
        article.workspace_id,
        "content/article-image",
        ImagePrompt,
        memory_query=f"image {article.title} {entry['concept']}",
        user_message="Write the image instructions.",
        article=ai.dumps(_article_view(article, entry)),
        image=ai.dumps({k: entry[k] for k in ("placement", "concept", "alt", "caption")}),
        style_done=ai.dumps(_done_style(article, index)),
        shape=SHAPE_NOTE["landscape"],
    )


def _file_name(text: str) -> str:
    return SAFE_NAME.sub("-", text.lower()).strip("-")[:60] or "article-image"


def make_one(article: Article, index: int) -> str:
    """Draw the image at ``index``; returns an error code or an empty string.

    A failed image is recorded; it never fails the article.
    """
    entry = article.images[index]
    try:
        spec = _ask(article, entry, index)
        entry["prompt"] = spec.prompt  # kept so a failed image can be drawn in another tool
        data = openai_images.generate(article.workspace_id, spec.prompt)
        name = _file_name(spec.filename)
        asset = _store(article, name, spec.alt or entry["alt"], data)
    except DomainError as exc:
        log.warning("article_image_failed", article=str(article.pk), code=exc.code.value)
        entry.update(status="failed", error=str(exc)[:200] or exc.code.value)
        return str(exc)
    entry.update(
        status="ready",
        error="",
        filename=f"{name}.webp",
        asset_id=str(asset.pk),
        alt=(spec.alt or entry["alt"])[:125],
        caption=spec.caption if spec.caption else entry["caption"],
        wp_media_id=None,
        wp_url="",
    )
    return ""


def fail_rest(article: Article, start: int, error: str) -> None:
    """Mark the images from ``start`` on as failed for the same reason."""
    for entry in article.images[start:]:
        if entry["status"] in {"planned", "queued"}:
            entry.update(status="failed", error=error)


def compress(data: bytes) -> bytes:
    """Re-encode as WebP at web quality and at most ``MAX_WIDTH`` wide (the raw file is >1 MB)."""
    with Image.open(io.BytesIO(data)) as picture:
        picture.load()
        target: Image.Image = picture
        if picture.width > MAX_WIDTH:
            height = round(picture.height * MAX_WIDTH / picture.width)
            target = picture.resize((MAX_WIDTH, height), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        target.save(out, "WEBP", quality=WEBP_QUALITY, method=6)
        return out.getvalue()


def _store(article: Article, name: str, alt: str, data: bytes) -> MediaAsset:
    asset, _ = media.create_from_chunks(
        article.workspace_id,
        None,
        [compress(data)],
        filename=f"{name}.webp",
        source=MediaSource.GENERATED,
        title=name.replace("-", " "),
        tags=["blog", "ai-generated"],
    )
    asset.alt_text = alt[:500]
    asset.ai_generated = True
    asset.rights = Rights.OWNED
    asset.save(update_fields=["alt_text", "ai_generated", "rights", "updated_at"])
    return asset


def prompt_for(article: Article, image_id: str) -> str:
    """The drawing prompt of one image; written now (text model only, no image cost) if missing."""
    index = next((i for i, e in enumerate(article.images) if e["id"] == image_id), None)
    if index is None:
        raise NotFoundError("image not found")
    entry = article.images[index]
    if not entry.get("prompt"):
        entry["prompt"] = _ask(article, entry, index).prompt
        article.save(update_fields=["images", "updated_at"])
    return str(entry["prompt"])


def attach_upload(article: Article, image_id: str, asset: MediaAsset) -> None:
    """Use a picture the person chose instead of a drawn one. It is sent to WordPress next."""
    entry = next((e for e in article.images if e["id"] == image_id), None)
    if entry is None:
        raise NotFoundError("image not found")
    if asset.kind != MediaKind.IMAGE or not asset.storage_key:
        raise ValidationFailedError("choose a picture file")
    name = _file_name(entry["alt"] or entry["concept"])
    entry.update(
        status="ready",
        error="",
        filename=f"{name}.webp",
        asset_id=str(asset.pk),
        wp_media_id=None,
        wp_url="",
    )
    if not asset.alt_text:
        asset.alt_text = entry["alt"][:500]
        asset.save(update_fields=["alt_text", "updated_at"])
    article.save(update_fields=["images", "updated_at"])


def ready_count(article: Article) -> int:
    return sum(1 for e in article.images if e["status"] == "ready")
