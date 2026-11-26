"""Turn a plain photo into a post picture by writing its headline on it."""

import dataclasses
from typing import Any

from media.models import MediaAsset
from media.storage import get_storage
from social import images
from studio.visuals.overlay import headline_on_photo, short_headline
from studio.visuals.ports import VisualRequest, VisualResult

TEXT_CARD = "text_card"
VARIANT = "headline"


def text_for(request: VisualRequest) -> str:
    """The line for the picture: the owner's chosen on-image text, else the post's hook."""
    chosen = request.brief.on_image_text.strip() or request.headline
    return short_headline(chosen)


def put_on(request: VisualRequest, entry: dict[str, Any], text: str) -> dict[str, Any]:
    """A copy of the picture of ``entry`` with ``text`` on it. The original is left untouched."""
    original = MediaAsset.objects.get(pk=entry["asset_id"], workspace_id=request.workspace_id)
    photo = get_storage().path(original.storage_key).read_bytes()
    size = (original.width or 1080, original.height or 1350)
    data = headline_on_photo(photo, text, palette=request.palette, size=size, handle=request.handle)
    made = images.store_picture(
        request.workspace_id,
        request.user_id,
        data,
        name=f"{request.title or 'Post'} cover"[:80],
        alt_text=str(entry.get("alt_text") or request.brief.alt_text),
        ai_generated=original.ai_generated,
    )
    made.derived_from = original
    made.variant = VARIANT
    made.save(update_fields=["derived_from", "variant", "updated_at"])
    return {"asset_id": str(made.pk), "alt_text": made.alt_text}


def apply(request: VisualRequest, result: VisualResult) -> VisualResult:
    """Headline on the first picture of ``result`` when the owner wants text on pictures.

    Branded text cards and designed covers already carry their words, so only plain photographs
    are touched. A problem drawing the text never loses the plain photo.
    """
    text = text_for(request)
    if request.style.cover_style == "plain" or result.has_text or not result.media or not text:
        return result
    try:
        first = put_on(request, result.media[0], text)
    except OSError, ValueError:
        return result
    return dataclasses.replace(result, media=[first, *result.media[1:]])
