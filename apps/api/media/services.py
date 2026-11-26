"""Create, import, link, derive and delete media assets."""

import io
import re
import uuid
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from PIL import Image, ImageOps

from core.errors import ConflictError, NotFoundError, ValidationFailedError
from core.security import UnsafeUrlError, safe_fetch
from media import inspect
from media.models import MediaAsset, MediaKind, MediaSource
from media.specs import CROPPABLE, SPEC_BY_KEY, Spec
from media.storage import MediaStorage, get_storage

YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
OEMBED = "https://www.youtube.com/oembed"
MAX_NAME = 300
MAX_TAGS = 30
MAX_TAG_CHARS = 40
JPEG_QUALITIES = (92, 88, 84, 80, 75, 70, 60, 50)


def _clean_tags(tags: Iterable[str] | None) -> list[str]:
    seen: list[str] = []
    for tag in tags or []:
        text = str(tag).strip().lower()[:MAX_TAG_CHARS]
        if text and text not in seen:
            seen.append(text)
    return seen[:MAX_TAGS]


def _name_from(filename: str, fallback: str) -> str:
    stem = filename.rsplit(".", 1)[0].strip() if filename else ""
    return (stem or fallback)[:MAX_NAME]


def _discard_duplicate(storage: MediaStorage, key: str, existing: MediaAsset) -> None:
    """Drop the copy just written for a duplicate file.

    Files are stored under their content hash, so the copy and the existing asset's file can be the
    same path; deleting it then would leave the existing asset without its file.
    """
    if existing.storage_key != key:
        storage.delete(key)


def _unique_violation(workspace_id: uuid.UUID, sha: str) -> MediaAsset | None:
    return MediaAsset.objects.filter(workspace_id=workspace_id, sha256=sha).first()


def create_from_chunks(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    chunks: Iterable[bytes],
    *,
    filename: str,
    source: str,
    title: str = "",
    tags: Iterable[str] | None = None,
    storage: MediaStorage | None = None,
) -> tuple[MediaAsset, bool]:
    """Store and inspect a file. Returns ``(asset, created)``; an identical file is reused."""
    storage = storage or get_storage()
    limit = settings.API_MAX_UPLOAD_BYTES
    try:
        key, sha, size = storage.save_stream(workspace_id, chunks, extension="", max_bytes=limit)
    except OverflowError as exc:
        raise ValidationFailedError("the file is too large") from exc
    existing = _unique_violation(workspace_id, sha)
    if existing:
        _discard_duplicate(storage, key, existing)
        return existing, False
    path = storage.path(key)
    try:
        if _looks_like_video(path):
            facts: inspect.ImageFacts | inspect.VideoFacts = inspect.inspect_video(path)
            kind = MediaKind.VIDEO
        else:
            if size > settings.MEDIA_MAX_IMAGE_BYTES:
                raise ValidationFailedError("the image file is too large")
            facts = inspect.inspect_image(path)
            kind = MediaKind.IMAGE
    except ValidationFailedError:
        storage.delete(key)
        raise
    final_key = f"{workspace_id}/{sha[:2]}/{sha}{facts.extension}"
    if final_key != key:
        storage.path(key).replace(storage.path(final_key))
        key = final_key
    asset = MediaAsset(
        workspace_id=workspace_id,
        kind=kind,
        source=source,
        name=title.strip()[:MAX_NAME] or _name_from(filename, "Untitled"),
        tags=_clean_tags(tags),
        storage_key=key,
        original_filename=filename[:300],
        mime_type=facts.mime_type,
        file_format=facts.file_format,
        size_bytes=size,
        sha256=sha,
        width=facts.width,
        height=facts.height,
        created_by_id=user_id,
    )
    if isinstance(facts, inspect.ImageFacts):
        asset.animated, asset.has_alpha = facts.animated, facts.has_alpha
    else:
        asset.duration_seconds = facts.duration_seconds
    try:
        with transaction.atomic():
            asset.save()
    except IntegrityError:
        storage.delete(key)
        found = _unique_violation(workspace_id, sha)
        if found is None:
            raise
        return found, False
    return asset, True


def _looks_like_video(path: Any) -> bool:
    with path.open("rb") as handle:
        head = handle.read(12)
    return bool(head[4:8] == b"ftyp" or head.startswith(inspect.WEBM_MAGIC))


def import_from_url(
    workspace_id: uuid.UUID, user_id: uuid.UUID | None, url: str, *, title: str = ""
) -> tuple[MediaAsset, bool]:
    """Fetch a public https image and store it. Never follows links to private networks."""
    try:
        response = safe_fetch(url, max_bytes=settings.MEDIA_MAX_IMAGE_BYTES, timeout=20.0)
    except UnsafeUrlError as exc:
        raise ValidationFailedError(str(exc)) from exc
    if response.status_code != 200:  # noqa: PLR2004
        raise ValidationFailedError("the link did not return an image")
    filename = urlsplit(url).path.rsplit("/", 1)[-1] or "image"
    return create_from_chunks(
        workspace_id,
        user_id,
        [response.content],
        filename=filename,
        source=MediaSource.URL,
        title=title,
    )


def parse_youtube_id(value: str) -> str:
    text = value.strip()
    if YOUTUBE_ID.match(text):
        return text
    parts = urlsplit(text if "//" in text else f"https://{text}")
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        candidate = parts.path.strip("/").split("/")[0]
    elif host in YOUTUBE_HOSTS:
        query = dict(pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair)
        segments = [s for s in parts.path.split("/") if s]
        candidate = query.get("v") or (
            segments[1]
            if len(segments) > 1 and segments[0] in {"shorts", "embed", "live", "v"}
            else ""
        )
    else:
        candidate = ""
    if not YOUTUBE_ID.match(candidate):
        raise ValidationFailedError("enter a YouTube link or an 11-character video id")
    return candidate


def link_youtube(
    workspace_id: uuid.UUID, user_id: uuid.UUID | None, value: str, *, title: str = ""
) -> tuple[MediaAsset, bool]:
    video_id = parse_youtube_id(value)
    existing = MediaAsset.objects.filter(
        workspace_id=workspace_id, youtube_video_id=video_id
    ).first()
    if existing:
        return existing, False
    watch = f"https://www.youtube.com/watch?v={video_id}"
    details: dict[str, Any] = {}
    try:
        response = safe_fetch(
            f"{OEMBED}?url={quote(watch, safe='')}&format=json",
            allowed_hosts=frozenset({"www.youtube.com"}),
            timeout=10.0,
            max_bytes=200_000,
        )
    except UnsafeUrlError:
        response = None
    if response is not None and response.status_code == 200:  # noqa: PLR2004
        details = {
            k: v
            for k, v in response.json().items()
            if k in {"title", "author_name", "thumbnail_url"}
        }
    elif response is not None and response.status_code in {401, 403, 404}:
        raise ValidationFailedError("that YouTube video is not available or is private")
    asset = MediaAsset.objects.create(
        workspace_id=workspace_id,
        kind=MediaKind.VIDEO,
        source=MediaSource.YOUTUBE,
        name=(title.strip() or details.get("title") or f"YouTube {video_id}")[:MAX_NAME],
        youtube_video_id=video_id,
        file_format="YOUTUBE",
        external=details,
        created_by_id=user_id,
    )
    return asset, True


# --- derived crops ----------------------------------------------------------------------------


def _crop_box(
    width: int, height: int, ratio: float, focal: tuple[float, float]
) -> tuple[int, int, int, int]:
    if width / height > ratio:
        crop_w, crop_h = round(height * ratio), height
    else:
        crop_w, crop_h = width, round(width / ratio)
    left = min(max(round(focal[0] * width - crop_w / 2), 0), width - crop_w)
    top = min(max(round(focal[1] * height - crop_h / 2), 0), height - crop_h)
    return left, top, left + crop_w, top + crop_h


def _encode(image: Image.Image, spec: Spec, keep_alpha: bool) -> tuple[bytes, str, str]:
    """Encode under the size cap. Logos with transparency stay PNG; everything else is JPEG."""
    cap = spec.max_bytes or settings.MEDIA_MAX_IMAGE_BYTES
    if keep_alpha and image.mode in {"RGBA", "LA", "P"}:
        buffer = io.BytesIO()
        image.convert("RGBA").save(buffer, "PNG", optimize=True)
        if buffer.tell() <= cap:
            return buffer.getvalue(), "PNG", ".png"
    flat = image.convert("RGB")
    for quality in JPEG_QUALITIES:
        buffer = io.BytesIO()
        flat.save(buffer, "JPEG", quality=quality, optimize=True)
        if buffer.tell() <= cap:
            return buffer.getvalue(), "JPEG", ".jpg"
    raise ValidationFailedError("the image cannot be made small enough for this destination")


def derive(
    source: MediaAsset,
    user_id: uuid.UUID | None,
    spec_key: str,
    *,
    focal: tuple[float, float] = (0.5, 0.5),
    storage: MediaStorage | None = None,
) -> tuple[MediaAsset, bool]:
    """Crop the source to a destination's ratio at its recommended size; the source is untouched."""
    spec = SPEC_BY_KEY.get(spec_key)
    if spec is None or spec not in CROPPABLE or spec.ratio is None or spec.min_size is None:
        raise ValidationFailedError("unknown crop")
    if source.kind != MediaKind.IMAGE or not source.storage_key:
        raise ValidationFailedError("only images can be cropped")
    if not all(0.0 <= v <= 1.0 for v in focal):
        raise ValidationFailedError("the focal point must be between 0 and 1")
    storage = storage or get_storage()
    with Image.open(storage.path(source.storage_key)) as opened:
        image = ImageOps.exif_transpose(opened)
        image.load()
    ratio = spec.ratio[0] / spec.ratio[1]
    box = _crop_box(image.width, image.height, ratio, focal)
    cropped = image.crop(box)
    target = spec.recommended or (cropped.width, cropped.height)
    if cropped.width >= target[0] and cropped.height >= target[1]:
        cropped = cropped.resize(target, Image.Resampling.LANCZOS)
    if cropped.width < spec.min_size[0] or cropped.height < spec.min_size[1]:
        raise ValidationFailedError(
            f"the image is too small for {spec.label}: "
            f"needs at least {spec.min_size[0]}x{spec.min_size[1]}"
        )
    data, file_format, extension = _encode(cropped, spec, keep_alpha="logo" in spec.key)
    key, sha, size = storage.save_stream(
        source.workspace_id, [data], extension=extension, max_bytes=settings.MEDIA_MAX_IMAGE_BYTES
    )
    existing = _unique_violation(source.workspace_id, sha)
    if existing:
        _discard_duplicate(storage, key, existing)
        return existing, False
    facts = inspect.inspect_image(storage.path(key))
    asset = MediaAsset.objects.create(
        workspace_id=source.workspace_id,
        kind=MediaKind.IMAGE,
        source=MediaSource.DERIVED,
        name=f"{source.name} - {spec.label}"[:MAX_NAME],
        alt_text=source.alt_text,
        tags=source.tags,
        storage_key=key,
        original_filename=f"{spec.key}{extension}",
        mime_type=facts.mime_type,
        file_format=file_format,
        size_bytes=size,
        sha256=sha,
        width=facts.width,
        height=facts.height,
        has_alpha=facts.has_alpha,
        rights=source.rights,
        license_note=source.license_note,
        license_expires_on=source.license_expires_on,
        ai_generated=source.ai_generated,
        derived_from=source,
        variant=spec.key,
        created_by_id=user_id,
    )
    return asset, True


def get(workspace_id: uuid.UUID, asset_id: uuid.UUID) -> MediaAsset:
    asset = MediaAsset.objects.filter(pk=asset_id, workspace_id=workspace_id).first()
    if asset is None:
        raise NotFoundError("asset not found")
    return asset


def delete(asset: MediaAsset, *, storage: MediaStorage | None = None) -> None:
    if asset.derivatives.exists():
        raise ConflictError("delete the crops made from this asset first")
    key = asset.storage_key
    asset.delete()
    if key and not MediaAsset.objects.filter(storage_key=key).exists():
        (storage or get_storage()).delete(key)
