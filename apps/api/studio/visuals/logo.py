"""Put the company logo on every picture, pixel-exact.

Image models cannot reproduce a logo faithfully, so the studio never asks them to draw one. For a
designed picture the model is told to leave a clean corner empty; the real logo file is then placed
there. If the logo would disappear into the background it gets a soft plate behind it.
"""

import hashlib
import io
import uuid
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageStat

from media.models import MediaAsset, MediaKind, MediaSource
from media.storage import get_storage
from social import images
from studio.config import VisualConfig
from studio.models import Brand

CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")
CARD_CORNER = "top-right"
MAX_WIDTH = 0.11
MAX_HEIGHT = 0.065
MARGIN = 0.035
WHITE_FULL = 245
WHITE_NONE = 225
PLATE_PAD = 0.012
MIN_CONTRAST = 3.0
PREFERENCE = 3.0
VARIANT = "logo"
LINEAR_CUTOFF = 0.03928
FULL_ALPHA = 255
MID_GREY = 0.5
VISIBLE_ALPHA = 40
LIGHT, DARK = (255, 255, 255), (15, 23, 42)


def _label(asset: MediaAsset) -> str:
    return f"{asset.name} {' '.join(map(str, asset.tags))}".lower()


def find(
    workspace_id: uuid.UUID, style: VisualConfig, brand_id: uuid.UUID | None = None
) -> MediaAsset | None:
    """The brand's logo: the file chosen in its settings, else an upload named or tagged logo.

    With several companies in one library, an upload counts as a company's logo when its name or
    tags also mention the company (its name or short name). A lone company may use any logo.
    """
    rows = MediaAsset.objects.filter(workspace_id=workspace_id, kind=MediaKind.IMAGE).exclude(
        storage_key=""
    )
    if style.logo_asset_id:
        return rows.filter(pk=style.logo_asset_id).first()
    logos = [
        a
        for a in rows.filter(source=MediaSource.UPLOAD).order_by("-created_at")[:200]
        if "logo" in _label(a)
    ]
    brand = Brand.objects.filter(pk=brand_id).first() if brand_id else None
    if brand is not None:
        names = {brand.slug.replace("-", ""), brand.name.lower().replace(" ", "")}
        mine = [
            a
            for a in logos
            if any(n and n in _label(a).replace(" ", "").replace("-", "") for n in names)
        ]
        if mine:
            return mine[0]
        others = Brand.objects.filter(workspace_id=workspace_id, archived_at__isnull=True).count()
        return logos[0] if logos and others <= 1 else None
    return logos[0] if logos else None


def corner_for(key: uuid.UUID) -> str:
    """A corner that varies from post to post but is stable for one post."""
    return CORNERS[hashlib.sha1(key.bytes).digest()[0] % len(CORNERS)]  # noqa: S324 - not security


def _luminance(colour: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= LINEAR_CUTOFF else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = colour
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(first: float, second: float) -> float:
    light, dark = max(first, second), min(first, second)
    return (light + 0.05) / (dark + 0.05)


def _logo_tone(logo: Image.Image) -> float:
    """Average brightness of the visible pixels of the logo."""
    raw = logo.tobytes()
    visible = [raw[i : i + 4] for i in range(0, len(raw), 4) if raw[i + 3] > VISIBLE_ALPHA]
    if not visible:
        return MID_GREY
    r, g, b = (round(sum(p[i] for p in visible) / len(visible)) for i in range(3))
    return _luminance((r, g, b))


def prepare(logo: Image.Image) -> Image.Image:
    """The logo as a tight, transparent mark.

    A logo saved on a white background gets that white removed (softly at the edges), and any
    empty margin around the visible mark is trimmed so the size is the size of the mark itself.
    """
    rgba = logo.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha.getextrema()[0] == FULL_ALPHA:
        red, green, blue = rgba.convert("RGB").split()
        lightest_channel = ImageChops.darker(ImageChops.darker(red, green), blue)
        span = WHITE_FULL - WHITE_NONE
        keyed = lightest_channel.point(
            lambda v: (
                0
                if v >= WHITE_FULL
                else FULL_ALPHA
                if v <= WHITE_NONE
                else round((WHITE_FULL - v) * FULL_ALPHA / span)
            )
        )
        rgba.putalpha(keyed)
    box = rgba.getchannel("A").getbbox()
    return rgba.crop(box) if box else rgba


def _position(corner: str, size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    margin = round(size[0] * MARGIN)
    x = margin if corner.endswith("left") else size[0] - box[0] - margin
    y = margin if corner.startswith("top") else size[1] - box[1] - margin
    return x, y


def _scaled(logo: Image.Image, size: tuple[int, int]) -> Image.Image:
    scale = min(size[0] * MAX_WIDTH / logo.width, size[1] * MAX_HEIGHT / logo.height)
    return logo.resize((max(round(logo.width * scale), 1), max(round(logo.height * scale), 1)))


def calmest_corner(picture: Image.Image, box: tuple[int, int], preferred: str) -> str:
    """The corner with the least going on in it (text, faces and objects are busy), so the logo
    covers as little as possible. The preferred corner wins close calls."""
    scores: dict[str, float] = {}
    for corner in CORNERS:
        x, y = _position(corner, picture.size, box)
        region = picture.crop((x, y, x + box[0], y + box[1])).convert("L")
        spread = ImageStat.Stat(region).stddev[0]
        scores[corner] = spread - (PREFERENCE if corner == preferred else 0.0)
    return min(scores, key=lambda corner: scores[corner])


def _compose(base: Image.Image, logo: Image.Image, corner: str) -> bytes:
    x, y = _position(corner, base.size, logo.size)
    under = ImageStat.Stat(base.crop((x, y, x + logo.width, y + logo.height)).convert("RGB"))
    mean = [round(v) for v in under.mean]
    background = _luminance((mean[0], mean[1], mean[2]))
    if _contrast(background, _logo_tone(logo)) < MIN_CONTRAST:
        pad = round(base.width * PLATE_PAD)
        plate = LIGHT if _logo_tone(logo) < MID_GREY else DARK
        draw = ImageDraw.Draw(base, "RGBA")
        draw.rounded_rectangle(
            (x - pad, y - pad, x + logo.width + pad, y + logo.height + pad),
            radius=pad,
            fill=(*plate, 235),
        )
    base.alpha_composite(logo, (x, y))
    buffer = io.BytesIO()
    base.convert("RGB").save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def _load(picture: bytes, logo_file: bytes) -> tuple[Image.Image, Image.Image]:
    with Image.open(io.BytesIO(picture)) as opened:
        base = opened.convert("RGBA")
    with Image.open(io.BytesIO(logo_file)) as opened:
        logo = _scaled(prepare(opened), base.size)
    return base, logo


def place(picture: bytes, logo_file: bytes, corner: str) -> bytes:
    """The picture with the logo in ``corner`` (PNG bytes)."""
    base, logo = _load(picture, logo_file)
    return _compose(base, logo, corner)


def place_in_calmest_corner(picture: bytes, logo_file: bytes, preferred: str) -> bytes:
    """The picture with the logo in its quietest corner (the preferred one on a close call)."""
    base, logo = _load(picture, logo_file)
    return _compose(base, logo, calmest_corner(base, logo.size, preferred))


def stamp(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    entry: dict[str, Any],
    logo: MediaAsset,
    corner: str,
    *,
    fixed: bool = False,
) -> dict[str, Any]:
    """A copy of the picture of ``entry`` carrying the logo; the original is kept.

    ``fixed`` puts the logo exactly in ``corner`` (for layouts the studio drew itself); otherwise
    ``corner`` is only a preference and the quietest corner of the picture is used.
    """
    original = MediaAsset.objects.get(pk=entry["asset_id"], workspace_id=workspace_id)
    storage = get_storage()
    picture = storage.path(original.storage_key).read_bytes()
    logo_file = storage.path(logo.storage_key).read_bytes()
    data = (
        place(picture, logo_file, corner)
        if fixed
        else place_in_calmest_corner(picture, logo_file, corner)
    )
    made = images.store_picture(
        workspace_id,
        user_id,
        data,
        name=f"{original.name} with logo"[:80],
        alt_text=str(entry.get("alt_text") or original.alt_text),
        ai_generated=original.ai_generated,
    )
    made.derived_from = original
    made.variant = VARIANT
    made.save(update_fields=["derived_from", "variant", "updated_at"])
    return {"asset_id": str(made.pk), "alt_text": made.alt_text}
