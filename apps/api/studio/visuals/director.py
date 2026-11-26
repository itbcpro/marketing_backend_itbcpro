"""Pick pictures and video for a piece, trying the owner's preferred sources in order.

A source that cannot help (not connected, no match, failed) hands over to the next one, and cards
drawn locally are the last resort, so a piece is never left without something to post unless the
owner turned pictures off. A failure never costs the written text.
"""

import dataclasses
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.utils import timezone

from core.errors import DomainError
from core.logging import get_logger
from media.models import MediaAsset, MediaSource
from social import ai, images
from studio import config_store
from studio.channels.registry import get_kind
from studio.channels.spec import CAROUSEL, IMAGE, TEXT, KindSpec
from studio.config import VisualConfig
from studio.models import ContentPiece
from studio.publishers.ports import account_for
from studio.schemas import PieceCopy
from studio.visuals import designer, headline, logo
from studio.visuals.ports import (
    VisualRequest,
    VisualResult,
    picture_provider,
    video_provider,
)

log = get_logger(__name__)

REUSE_WINDOW = timedelta(days=60)
HEX = re.compile(r"#[0-9a-fA-F]{6}")
IMAGE_KIND = "image"
LAST_RESORT = "text_card"


@dataclass(slots=True)
class Outcome:
    media: list[dict[str, Any]] = field(default_factory=list)
    cover_asset_id: uuid.UUID | None = None
    video_asset_id: uuid.UUID | None = None
    errors: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)


def recently_used(brand_id: uuid.UUID) -> frozenset[uuid.UUID]:
    since = timezone.now() - REUSE_WINDOW
    used: set[uuid.UUID] = set()
    for media in ContentPiece.objects.filter(brand_id=brand_id, created_at__gte=since).values_list(
        "media", flat=True
    ):
        for entry in media or []:
            try:
                used.add(uuid.UUID(str(entry.get("asset_id"))))
            except ValueError:
                continue
    frontier = set(used)
    while frontier:  # a crop, or a crop with a headline, uses its original
        parents = MediaAsset.objects.filter(pk__in=frontier, derived_from__isnull=False)
        frontier = set(parents.values_list("derived_from_id", flat=True)) - used
        used.update(frontier)
    return frozenset(used)


def _palette(piece: ContentPiece, visuals: VisualConfig) -> images.Palette:
    """The brand's own colours when the settings give them, else the workspace brand profile."""
    colours = [c for c in visuals.brand_colours if HEX.fullmatch(c)]
    if not colours:
        return images.palette_from(ai.brand_profile_content(piece.workspace_id))
    background = colours[0]
    accent = colours[1] if len(colours) > 1 else images.DEFAULT_PALETTE[1]
    return images.Palette(background, accent, images.readable_on(background))


def _shape(kind: KindSpec, cover_style: str) -> KindSpec:
    return designer.feed_shape(kind) if cover_style == "designed" else kind


def build_request(piece: ContentPiece, copy: PieceCopy, user_id: uuid.UUID | None) -> VisualRequest:
    config = config_store.load(piece.brand_id)
    plan = config.plan_for(piece.channel)
    account = account_for(piece.workspace_id, piece.channel, plan.account_id)
    has_logo = config.visuals.logo_on_pictures and logo.find(
        piece.workspace_id, config.visuals, piece.brand_id
    )
    return VisualRequest(
        workspace_id=piece.workspace_id,
        user_id=user_id,
        channel=piece.channel,
        kind=_shape(get_kind(piece.channel, piece.kind), config.visuals.cover_style),
        title=piece.title or copy.title,
        headline=copy.hook or copy.title,
        slides=list(copy.slides),
        brief=copy.visual,
        palette=_palette(piece, config.visuals),
        brand_id=piece.brand_id,
        style=config.visuals,
        handle=account.handle if account else "",
        logo_corner=logo.corner_for(piece.pk) if has_logo else "",
        recently_used=recently_used(piece.brand_id),
    )


def _order(preferred: list[str]) -> list[str]:
    return [*preferred, *([] if LAST_RESORT in preferred else [LAST_RESORT])]


def pictures(
    request: VisualRequest, kind: KindSpec, skip: frozenset[str] = frozenset()
) -> tuple[VisualResult | None, list[str]]:
    """The first source that produces pictures for ``kind``, and why earlier ones did not."""
    errors: list[str] = []
    asked = dataclasses.replace(request, kind=kind)
    for key in _order(list(request.style.prefer)):
        provider = picture_provider(key)
        if key in skip or provider is None or not provider.supports(kind):
            continue
        if not provider.available(request.workspace_id):
            errors.append(f"{key}:unavailable")
            continue
        try:
            return dataclasses.replace(provider.make(asked), source=key), errors
        except DomainError as exc:
            log.warning("studio_visual_failed", provider=key, code=exc.code.value)
            errors.append(f"{key}:{exc.code.value}")
    return None, errors


def _video(
    request: VisualRequest, copy: PieceCopy, previous: uuid.UUID | None
) -> tuple[uuid.UUID | None, list[str]]:
    if copy.video is None:
        return None, ["video:no_plan"]
    provider = video_provider(request.style.video_provider)
    if provider is None or not provider.available(request.workspace_id):
        return None, ["video:unavailable"]
    try:
        return provider.make(request, copy.video, previous=previous), []
    except DomainError as exc:
        return None, [f"video:{exc.code.value}"]


def _cover(
    request: VisualRequest,
) -> tuple[uuid.UUID | None, list[str], tuple[dict[str, Any], ...]]:
    still = dataclasses.replace(
        request.kind, kind=IMAGE, needs_video=False, min_media=1, max_media=1
    )
    result, errors = pictures(request, still)
    if result is None or not result.media:
        return None, errors, ()
    result = headline.apply(request, result)
    return uuid.UUID(str(result.media[0]["asset_id"])), errors, result.trace


INNER_SLIDE = (
    "Exception for this slide: it is an inner slide of a carousel, so use a clean, calm designed "
    "background in the brand colours instead of a photograph (no people, no scene), with the "
    "same bold typography style as the cover. The text is the hero. Slide {number} of {total}."
)
MAX_SUPPORT = 110


def _designed_slides(request: VisualRequest, kind: KindSpec, cards: VisualResult) -> VisualResult:
    """Carousel slides drawn as finished designs by the image model, when the owner chose that.

    A slide the model never spells right stays the exact text card it already is.
    """
    if request.style.slides_style != "designed" or not request.slides:
        return cards
    still = dataclasses.replace(kind, kind=IMAGE, min_media=1, max_media=1)
    asked = dataclasses.replace(request, kind=still)
    media = list(cards.media)
    trace: list[dict[str, Any]] = []
    total = len(request.slides)
    for index, slide in enumerate(request.slides):
        support = slide.body if len(slide.body) <= MAX_SUPPORT else ""
        note = INNER_SLIDE.format(number=index + 1, total=total)
        lines = [line for line in (slide.headline, support) if line]
        made = designer.design(asked, lines, note, trace)
        if made is not None and index < len(media):
            media[index] = {**made.media[0], "alt_text": slide.alt_text or slide.headline}
    return dataclasses.replace(cards, media=media, trace=(*cards.trace, *trace))


def _photo_cover(request: VisualRequest, kind: KindSpec, slides: VisualResult) -> VisualResult:
    """A carousel opens with a photograph carrying the hook; the other slides stay text cards."""
    if request.style.cover_style == "plain" or not slides.media:
        return slides
    still = dataclasses.replace(kind, kind=IMAGE, min_media=1, max_media=1)
    photo, _ = pictures(request, still, skip=frozenset({headline.TEXT_CARD}))
    if photo is None or not photo.media:
        return slides
    opener = request.slides[0].headline if request.slides else request.headline
    cover = headline.apply(dataclasses.replace(request, headline=opener), photo)
    return dataclasses.replace(
        slides, media=[cover.media[0], *slides.media[1:]], trace=(*slides.trace, *photo.trace)
    )


def _stamp(request: VisualRequest, outcome: Outcome) -> Outcome:
    """The company logo on every picture of the outcome (never on a video file)."""
    found = (
        logo.find(request.workspace_id, request.style, request.brand_id)
        if request.logo_corner
        else None
    )
    if found is None:
        return outcome

    def put(entry: dict[str, Any]) -> dict[str, Any]:
        asset = MediaAsset.objects.get(pk=entry["asset_id"])
        if asset.kind != IMAGE_KIND:
            return entry
        plain_layout = asset.variant == headline.VARIANT or (
            asset.source == MediaSource.GENERATED and not asset.ai_generated
        )
        corner = logo.CARD_CORNER if plain_layout else request.logo_corner
        try:
            return logo.stamp(
                request.workspace_id, request.user_id, entry, found, corner, fixed=plain_layout
            )
        except OSError, ValueError:
            return entry

    outcome.media = [put(entry) for entry in outcome.media]
    if outcome.cover_asset_id:
        covered = put({"asset_id": str(outcome.cover_asset_id), "alt_text": ""})
        outcome.cover_asset_id = uuid.UUID(covered["asset_id"])
    return outcome


def make(
    piece: ContentPiece,
    copy: PieceCopy,
    user_id: uuid.UUID | None,
    *,
    previous_video: uuid.UUID | None = None,
) -> Outcome:
    """Pictures or video for ``piece``; the outcome lists what was tried when something failed."""
    request = build_request(piece, copy, user_id)
    kind = request.kind
    if kind.kind == TEXT:
        return Outcome()
    if kind.needs_video:
        video_id, errors = _video(request, copy, previous_video)
        cover, cover_errors, trace = _cover(request)
        media = [{"asset_id": str(video_id), "alt_text": ""}] if video_id else []
        return _stamp(
            request, Outcome(media, cover, video_id, [*errors, *cover_errors], list(trace))
        )
    result, errors = pictures(request, kind)
    if result is not None and kind.kind == CAROUSEL:
        result = _photo_cover(request, kind, _designed_slides(request, kind, result))
    elif result is not None:
        result = headline.apply(request, result)
    return _stamp(
        request,
        Outcome(
            result.media if result else [],
            result.cover_asset_id if result else None,
            None,
            errors,
            list(result.trace) if result else [],
        ),
    )
