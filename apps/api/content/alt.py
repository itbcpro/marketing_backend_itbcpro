"""Alt text for the images of a page: find the ones without it, let Claude look at them, write.

Images are fetched through ``net.fetch`` (https and public addresses only) and sent to the model
as pictures. Text inside a picture and the page text are data; the task has no tools.
"""

import base64
import re
from contextlib import suppress
from typing import Any
from urllib.parse import unquote, urlsplit

from content import ai, net
from content.models import ContentItem
from content.wordpress import factory
from core.errors import DomainError
from core.logging import get_logger
from llm.ports import ContentBlock, OpaqueBlock, TextBlock

log = get_logger(__name__)

MAX_PER_CALL = 10
MAX_IMAGE_BYTES = 3_500_000
VISION_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_ALT = 125


def missing(item: ContentItem) -> list[dict[str, Any]]:
    """Images of the page that have no alt text."""
    return [i for i in item.images if not str(i.get("alt", "")).strip()]


def file_name(src: str) -> str:
    return unquote(urlsplit(src).path.rsplit("/", 1)[-1])


SIZE_SUFFIX = re.compile(r"-\d+x\d+(?=\.[^.]+$)")
MAX_LOOKUPS = 20


def file_key(src: str) -> str:
    """The file a picture comes from, without the ``-1024x756`` size suffix WordPress adds."""
    return SIZE_SUFFIX.sub("", file_name(src)).lower()


def _same_site(item: ContentItem, src: str) -> bool:
    host = (urlsplit(src).hostname or "").lower().removeprefix("www.")
    own = (urlsplit(item.website.base_url).hostname or "").lower().removeprefix("www.")
    return host == own and "/wp-content/uploads/" in urlsplit(src).path


def _library_id(client: Any, key: str) -> int | None:
    """The one library entry whose file is ``key``; ``None`` when there is none or several."""
    stem = key.rsplit(".", 1)[0]
    for query in ({"slug": stem}, {"search": stem}):
        ids = {
            int(row["id"])
            for row in client.find_media(**query)
            if row.get("id") and file_key(str(row.get("source_url", ""))) == key
        }
        if len(ids) == 1:
            return ids.pop()
        if ids:
            return None
    return None


def resolve_attachments(item: ContentItem) -> bool:
    """Give pictures without a ``wp-image-<id>`` class their library entry, found by file name.

    Page builders leave the id out of the markup. A picture is only matched when exactly one
    library file has that name; anything else stays unmatched (and is refused when applying).
    """
    todo = {
        file_key(str(i["src"])): None
        for i in item.images
        if not i.get("attachment_id") and _same_site(item, str(i["src"]))
    }
    if not todo or item.website.connection is None:
        return False
    client = factory.for_connection(item.website.connection)
    for key in list(todo)[:MAX_LOOKUPS]:
        with suppress(DomainError):
            todo[key] = _library_id(client, key)
    changed = False
    for image in item.images:
        found = todo.get(file_key(str(image["src"])))
        if not image.get("attachment_id") and found:
            image["attachment_id"] = found
            changed = True
    if changed:
        item.save(update_fields=["images", "updated_at"])
    return changed


def _picture(src: str) -> OpaqueBlock | None:
    """The image as a model input, or ``None`` when it cannot be read (SVG, too large, blocked)."""
    try:
        response = net.fetch(src, max_bytes=MAX_IMAGE_BYTES)
    except (DomainError, Exception) as exc:
        log.info("alt_image_unreadable", error=type(exc).__name__)
        return None
    media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if response.status_code != 200 or media_type not in VISION_TYPES:  # noqa: PLR2004
        return None
    data = base64.standard_b64encode(response.content).decode()
    return OpaqueBlock(
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    )


def suggest(item: ContentItem, srcs: list[str] | None = None) -> list[dict[str, Any]]:
    """Alt text suggestions for up to ``MAX_PER_CALL`` images without alt text."""
    resolve_attachments(item)
    wanted = [i for i in missing(item) if srcs is None or i["src"] in srcs][:MAX_PER_CALL]
    if not wanted:
        return []
    blocks: list[ContentBlock] = [TextBlock("Write the alt text for these images.")]
    described: list[dict[str, Any]] = []
    seen: list[bool] = []
    for number, image in enumerate(wanted, start=1):
        picture = _picture(str(image["src"]))
        seen.append(picture is not None)
        described.append(
            {
                "index": number,
                "file": file_name(str(image["src"])),
                "heading_before": image.get("heading", ""),
                "text_before": image.get("near", ""),
                "size": f"{image.get('width') or '?'}x{image.get('height') or '?'}",
                "picture_available": picture is not None,
            }
        )
        blocks.append(TextBlock(f"Image {number}:"))
        if picture is not None:
            blocks.append(picture)
        else:
            blocks.append(TextBlock("(the picture was not available)"))
    result = ai.run_prompt(
        item.workspace_id,
        "content/image-alt",
        ai.AltSuggestions,
        memory_query=f"accessibility alt text {item.title}",
        user_message="",
        user_blocks=blocks,
        page=ai.untrusted(
            "website page",
            {"title": item.title, "url": item.url, "headings": item.outline[:15]},
        ),
        images=ai.untrusted("website images", described),
    )
    by_index = {entry.index: entry for entry in result.items}
    out: list[dict[str, Any]] = []
    for number, image in enumerate(wanted, start=1):
        entry = by_index.get(number)
        alt = "" if entry is None or entry.decorative else " ".join(entry.alt.split())[:MAX_ALT]
        out.append(
            {
                "src": image["src"],
                "attachment_id": image.get("attachment_id"),
                "suggested": alt,
                "decorative": bool(entry and entry.decorative),
                "note": entry.note if entry else "",
                "seen": seen[number - 1],
            }
        )
    return out
