"""Action kind for changing a page on the website: title, slug, SEO title and meta description.

Changing a public page is R2: a person approves it (the page dashboard proposes and approves in
one step for the signed-in owner, so the audit trail is complete either way). The executor reads
the live page first, refuses when someone changed it since the owner looked, and keeps the
previous values in the result so the change can be rolled back exactly.
"""

import html
import re
import uuid
from contextlib import suppress
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator

from actions.models import ProposedAction, RiskClass
from actions.registry import ActionKind, register_action_kind
from content import alt as alt_text
from content import review, seo_meta
from content.models import ContentItem, ItemSource
from content.sync import parse_gmt, refresh_item, rest_base_for, wp_seo
from content.wordpress import factory
from core.errors import DomainError, NotFoundError, PermanentProviderError

KIND = "content.update_page"
MEDIA_KIND = "content.update_media_alt"
MAX_ALTS = 30
MAX_ALT = 125
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CLOCK_SKEW = timedelta(seconds=2)
IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
IMG_MEDIA_ID = re.compile(r"""\bclass=["'][^"']*\bwp-image-(\d+)\b""", re.IGNORECASE)
IMG_SRC = re.compile(r"""(?<![\w-])src=(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
IMG_ALT = re.compile(r"""(?<![\w-])alt=(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
EMPTY_ALLOWED = {"seo_title", "meta_description"}


class UpdatePage(BaseModel):
    website_id: uuid.UUID
    item_id: uuid.UUID
    changes: dict[str, str] = Field(min_length=1)
    base_modified_at: str = ""
    """The page's last-modified time (UTC, ISO) when the owner looked at it."""
    suggestion_fields: list[str] = Field(default_factory=list)

    @field_validator("changes")
    @classmethod
    def _allowed(cls, value: dict[str, str]) -> dict[str, str]:
        clean: dict[str, str] = {}
        for field, raw in value.items():
            if field not in seo_meta.LIMITS:
                raise ValueError(f"{field} cannot be changed")
            text = " ".join(raw.split()) if field != "slug" else raw.strip()
            if not text and field not in EMPTY_ALLOWED:
                raise ValueError(f"{field} cannot be empty")
            if len(text) > seo_meta.LIMITS[field]:
                raise ValueError(f"{field} is too long")
            if field == "slug" and not SLUG.fullmatch(text):
                raise ValueError("slug may only use lower case letters, digits and hyphens")
            clean[field] = text
        return clean


def _item(action: ProposedAction) -> ContentItem:
    payload = UpdatePage.model_validate(action.effective_payload)
    item = (
        ContentItem.objects.select_related("website__connection")
        .filter(
            pk=payload.item_id,
            website_id=payload.website_id,
            workspace_id=action.workspace_id,
            source=ItemSource.WORDPRESS,
        )
        .first()
    )
    if item is None or item.website.connection is None:
        raise NotFoundError("this page cannot be changed through WordPress")
    return item


def _live_values(row: dict[str, Any], keys: dict[str, str]) -> dict[str, str]:
    raw_meta = row.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    seo = row.get("yoast_head_json") or {}
    return {
        "title": str((row.get("title") or {}).get("raw", "")),
        "slug": str(row.get("slug", "")),
        "seo_title": str(meta.get(keys.get("seo_title", ""), "")),
        "meta_description": str(meta.get(keys.get("meta_description", ""), "")),
        "_rendered_seo_title": str(seo.get("title", "")),
        "_rendered_description": str(seo.get("description", "")),
    }


def _check_conflict(row: dict[str, Any], payload: UpdatePage) -> None:
    base = parse_gmt(payload.base_modified_at)
    live = parse_gmt(row.get("modified_gmt"))
    if base is not None and live is not None and live > base + CLOCK_SKEW:
        raise PermanentProviderError("page_changed_on_site", provider="wordpress")


def _body(payload: UpdatePage, keys: dict[str, str]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    meta: dict[str, str] = {}
    for field, text in payload.changes.items():
        if field in seo_meta.SEO_FIELDS:
            meta[keys[field]] = text
        else:
            body[field] = text
    if meta:
        body["meta"] = meta
    return body


def _require_writable(payload: UpdatePage, keys: dict[str, str], row: dict[str, Any]) -> None:
    wanted = [f for f in payload.changes if f in seo_meta.SEO_FIELDS]
    exposed = set(row.get("meta") or {}) if isinstance(row.get("meta"), dict) else set()
    if wanted and not all(keys.get(f) in exposed for f in wanted):
        raise PermanentProviderError("seo_fields_not_writable", provider="wordpress")


def _refresh_local(item: ContentItem, row: dict[str, Any]) -> None:
    item.title = " ".join(str((row.get("title") or {}).get("rendered", item.title)).split())[:500]
    item.slug = str(row.get("slug", item.slug))
    item.url = str(row.get("link", item.url))
    item.modified_at = parse_gmt(row.get("modified_gmt")) or item.modified_at
    seo = wp_seo(row)
    if seo:
        item.seo = seo
    item.save()


def _remember_written_seo(item: ContentItem, payload: UpdatePage) -> None:
    """SEO plugins refresh their cached output with a delay; what we just wrote is the truth."""
    written = {
        "title": payload.changes.get("seo_title"),
        "description": payload.changes.get("meta_description"),
    }
    seo = {**item.seo, **{k: v for k, v in written.items() if v is not None}}
    if seo != item.seo:
        item.seo = seo
        item.save(update_fields=["seo", "updated_at"])


def execute(action: ProposedAction) -> dict[str, Any]:
    payload = UpdatePage.model_validate(action.effective_payload)
    item = _item(action)
    client = factory.for_connection(item.website.connection)  # type: ignore[arg-type]
    base = rest_base_for(client, item)
    keys = seo_meta.seo_keys(item.website)
    live = client.item(base, int(item.external_id))
    _check_conflict(live, payload)
    _require_writable(payload, keys, live)
    before = {k: v for k, v in _live_values(live, keys).items() if k in payload.changes}
    saved = client.update_item(base, int(item.external_id), _body(payload, keys))
    _refresh_local(item, saved)
    _remember_written_seo(item, payload)
    review.mark_applied(item.pk, list(payload.changes))
    return {
        "before": before,
        "after": dict(payload.changes),
        "modified_gmt": str(saved.get("modified_gmt", "")),
    }


def verify(action: ProposedAction) -> bool:
    payload = UpdatePage.model_validate(action.effective_payload)
    item = _item(action)
    client = factory.for_connection(item.website.connection)  # type: ignore[arg-type]
    keys = seo_meta.seo_keys(item.website)
    live = _live_values(client.item(rest_base_for(client, item), int(item.external_id)), keys)
    return all(live.get(field) == text for field, text in payload.changes.items())


def preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = UpdatePage.model_validate(action.payload)
    item = ContentItem.objects.filter(pk=payload.item_id, workspace_id=action.workspace_id).first()
    if item is None:
        return None, dict(payload.changes)
    return (
        {field: review.current_value(item, field) for field in payload.changes},
        dict(payload.changes),
    )


def rollback_payload(action: ProposedAction) -> dict[str, Any]:
    payload = UpdatePage.model_validate(action.effective_payload)
    before = (action.result or {}).get("before", {})
    return {
        "website_id": str(payload.website_id),
        "item_id": str(payload.item_id),
        "changes": {k: str(v) for k, v in before.items() if k in payload.changes},
        "base_modified_at": str((action.result or {}).get("modified_gmt", "")),
    }


class UpdateMediaAlt(BaseModel):
    """New alt text for images of the media library, shown on one page."""

    website_id: uuid.UUID
    item_id: uuid.UUID
    alts: dict[str, str] = Field(min_length=1, max_length=MAX_ALTS)

    @field_validator("alts")
    @classmethod
    def _clean(cls, value: dict[str, str]) -> dict[str, str]:
        clean: dict[str, str] = {}
        for media_id, raw in value.items():
            if not media_id.isdigit():
                raise ValueError("media ids are numbers")
            text = " ".join(raw.split())
            if len(text) > MAX_ALT:
                raise ValueError(f"alt text is too long (at most {MAX_ALT} characters)")
            clean[media_id] = text
        return clean


def _alt_item(action: ProposedAction) -> ContentItem:
    payload = UpdateMediaAlt.model_validate(action.effective_payload)
    item = (
        ContentItem.objects.select_related("website__connection")
        .filter(
            pk=payload.item_id,
            website_id=payload.website_id,
            workspace_id=action.workspace_id,
            source=ItemSource.WORDPRESS,
        )
        .first()
    )
    if item is None or item.website.connection is None:
        raise NotFoundError("this page cannot be changed through WordPress")
    return item


def _body_alt_sites(body: str, files: dict[str, str]) -> list[tuple[re.Match[str], str, str]]:
    """Every ``<img>`` of the stored body that is a library picture: tag, id, current alt.

    The id comes from the ``wp-image-<id>`` class, or else from the file name (``files`` maps a
    file name to its library id).
    """
    found = []
    for tag in IMG_TAG.finditer(body):
        picked = IMG_MEDIA_ID.search(tag.group(0))
        source = IMG_SRC.search(tag.group(0))
        media_id = (
            picked.group(1)
            if picked
            else files.get(alt_text.file_key(source.group(2)), "")
            if source
            else ""
        )
        if not media_id:
            continue
        written = IMG_ALT.search(tag.group(0))
        found.append((tag, media_id, html.unescape(written.group(2)) if written else ""))
    return found


def _library_files(client: Any, alts: dict[str, str]) -> dict[str, str]:
    """``{file name: media id}`` of the pictures being changed."""
    files: dict[str, str] = {}
    for media_id in alts:
        address = str(client.media(int(media_id)).get("source_url", ""))
        if address:
            files[alt_text.file_key(address)] = media_id
    return files


def _patched_body(
    body: str, alts: dict[str, str], before: dict[str, str], files: dict[str, str]
) -> str:
    """The body with the new alt text written into its pictures.

    Block-editor bodies carry the alt text inside every ``<img>``, so changing the media library
    alone leaves the page as it was. A picture is only changed when its alt text is empty or still
    the old library text, so a text someone wrote by hand for that spot is never overwritten.
    """
    out: list[str] = []
    last = 0
    for tag, media_id, current in _body_alt_sites(body, files):
        text = alts.get(media_id)
        if text is None or current == text or current.strip() not in {"", before.get(media_id)}:
            continue
        attribute = f'alt="{html.escape(text, quote=True)}"'
        markup = tag.group(0)
        markup = (
            IMG_ALT.sub(lambda _m, a=attribute: a, markup, count=1)
            if IMG_ALT.search(markup)
            else markup.replace("<img", f"<img {attribute}", 1)
        )
        out += [body[last : tag.start()], markup]
        last = tag.end()
    return "".join(out) + body[last:]


def _stored_body(client: Any, item: ContentItem) -> tuple[str, str] | None:
    """The raw body of a page whose pictures live in it (not a page-builder page)."""
    base = rest_base_for(client, item)
    row = client.item_body(base, int(item.external_id))
    meta = row.get("meta")
    if isinstance(meta, dict) and meta.get("_elementor_edit_mode"):
        return None  # the builder keeps its own copy; the media library is what it reads
    return base, str((row.get("content") or {}).get("raw", ""))


def execute_alt(action: ProposedAction) -> dict[str, Any]:
    payload = UpdateMediaAlt.model_validate(action.effective_payload)
    item = _alt_item(action)
    client = factory.for_connection(item.website.connection)  # type: ignore[arg-type]
    before: dict[str, str] = {}
    for media_id, text in payload.alts.items():
        before[media_id] = str(client.media(int(media_id)).get("alt_text", ""))
        client.set_media_alt(int(media_id), text)
    stored = _stored_body(client, item)
    if stored is not None:
        base, body = stored
        patched = _patched_body(body, payload.alts, before, _library_files(client, payload.alts))
        if patched != body:
            client.update_item(base, int(item.external_id), {"content": patched})
    with suppress(DomainError):
        refresh_item(item)  # the page itself is read again so the score and the checks update
    return {"before": before, "after": dict(payload.alts), "modified_gmt": ""}


def verify_alt(action: ProposedAction) -> bool:
    payload = UpdateMediaAlt.model_validate(action.effective_payload)
    item = _alt_item(action)
    client = factory.for_connection(item.website.connection)  # type: ignore[arg-type]
    if not all(
        str(client.media(int(media_id)).get("alt_text", "")) == text
        for media_id, text in payload.alts.items()
    ):
        return False
    stored = _stored_body(client, item)
    if stored is None:
        return True
    before = (action.result or {}).get("before", {})
    return all(
        current == payload.alts[media_id] or current.strip() not in {"", before.get(media_id)}
        for _tag, media_id, current in _body_alt_sites(
            stored[1], _library_files(client, payload.alts)
        )
        if media_id in payload.alts
    )


def preview_alt(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    payload = UpdateMediaAlt.model_validate(action.payload)
    item = ContentItem.objects.filter(pk=payload.item_id, workspace_id=action.workspace_id).first()
    current = {
        str(i.get("attachment_id")): str(i.get("alt", "")) for i in (item.images if item else [])
    }
    return {k: current.get(k, "") for k in payload.alts}, dict(payload.alts)


def rollback_alt(action: ProposedAction) -> dict[str, Any]:
    payload = UpdateMediaAlt.model_validate(action.effective_payload)
    before = (action.result or {}).get("before", {})
    return {
        "website_id": str(payload.website_id),
        "item_id": str(payload.item_id),
        "alts": {k: str(v) for k, v in before.items()},
    }


def register_action_kinds() -> None:
    register_action_kind(
        ActionKind(
            kind=MEDIA_KIND,
            payload_model=UpdateMediaAlt,
            risk=RiskClass.R2,
            executor=execute_alt,
            description="Set the alt text of images in the media library.",
            preview=preview_alt,
            verify=verify_alt,
            rollback_kind=MEDIA_KIND,
            rollback_payload=rollback_alt,
        )
    )
    register_action_kind(
        ActionKind(
            kind=KIND,
            payload_model=UpdatePage,
            risk=RiskClass.R2,
            executor=execute,
            description="Change the title, address, SEO title or meta description of a page.",
            preview=preview,
            verify=verify,
            rollback_kind=KIND,
            rollback_payload=rollback_payload,
        )
    )
