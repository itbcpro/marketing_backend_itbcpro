"""Pictures for posts: AI image generation behind a port, plus branded text cards drawn locally.

Generated pictures are stored as ``MediaAsset`` rows flagged ``ai_generated`` so the composer can
show the AI disclosure check. Text cards (quote cards, carousel slides) are drawn with Pillow from
the brand colours, so their text is always exact, which image models cannot promise.
"""

import base64
import io
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from django.conf import settings
from PIL import Image, ImageDraw, ImageFont, ImageOps

from connections.models import Connection, ConnectionStatus, CredentialKind
from connections.providers import (
    AuthType,
    FormField,
    HealthResult,
    ProviderDescriptor,
    register_provider,
)
from connections.services import read_secret
from core.errors import (
    AuthExpiredError,
    DomainError,
    NotFoundError,
    PermanentProviderError,
    PolicyRejectedError,
    RateLimitedError,
    RetryableProviderError,
    ValidationFailedError,
)
from media import services as media_services
from media.models import MediaAsset, MediaKind, MediaSource
from media.storage import get_storage

PROVIDER = "openai_images"
OPENAI_HOST = "https://api.openai.com/v1"
TIMEOUT = httpx.Timeout(180.0, connect=5.0)
SIZES = {"portrait": "1024x1536", "square": "1024x1024", "landscape": "1536x1024"}
CARD_SIZES = {"feed": (1080, 1350), "square": (1080, 1080), "story": (1080, 1920)}
DEFAULT_PALETTE = ("#0F172A", "#2563EB", "#F8FAFC")
HEX = re.compile(r"#[0-9a-fA-F]{6}")
FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "Arial Bold.ttf",
    "arialbd.ttf",
    "segoeuib.ttf",
    "Helvetica.ttc",
)
BODY_FONT_CANDIDATES = ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "segoeui.ttf", "Helvetica.ttc")
MAX_PROMPT_CHARS = 4000


class ImageGenerator(Protocol):
    """A vendor that turns a prompt into one picture (PNG or JPEG bytes)."""

    name: str

    def generate(self, prompt: str, *, orientation: str) -> bytes: ...


class OpenAiImageGenerator:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._model = model or settings.IMAGE_MODEL
        self._transport = transport

    def generate(self, prompt: str, *, orientation: str) -> bytes:
        body = {
            "model": self._model,
            "prompt": prompt[:MAX_PROMPT_CHARS],
            "size": SIZES.get(orientation, SIZES["square"]),
            "quality": settings.IMAGE_QUALITY,
            "n": 1,
        }
        try:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport) as client:
                response = client.post(
                    f"{OPENAI_HOST}/images/generations",
                    json=body,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
        except httpx.TransportError as exc:
            raise RetryableProviderError("image service is unreachable", provider=PROVIDER) from exc
        _raise_for(response)
        item = (response.json().get("data") or [{}])[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            raise PermanentProviderError(
                "the image service returned a link, not a file", provider=PROVIDER
            )
        raise PermanentProviderError("the image service returned nothing", provider=PROVIDER)


def _raise_for(response: httpx.Response) -> None:
    status = response.status_code
    if status < httpx.codes.BAD_REQUEST:
        return
    detail = f"HTTP {status}: {response.text[:200]}"
    if status in {401, 403}:
        raise AuthExpiredError(detail, provider=PROVIDER)
    if status == httpx.codes.TOO_MANY_REQUESTS:
        if "insufficient_quota" in response.text:
            # Billing, not speed: waiting does not help until credits are added.
            raise PermanentProviderError(
                "The image service has no credits left. Add credits to the OpenAI account.",
                provider=PROVIDER,
            )
        raise RateLimitedError(detail, provider=PROVIDER)
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise RetryableProviderError(detail, provider=PROVIDER)
    if "content_policy" in response.text or "safety" in response.text:
        raise PolicyRejectedError(detail, provider=PROVIDER)
    raise PermanentProviderError(detail, provider=PROVIDER)


class FakeImageGenerator:
    """Deterministic generator for tests and offline development."""

    name = "fake"

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []

    def generate(self, prompt: str, *, orientation: str) -> bytes:
        self.prompts.append((prompt, orientation))
        width, height = {"portrait": (1024, 1536), "landscape": (1536, 1024)}.get(
            orientation, (1024, 1024)
        )
        shade = 37 + len(self.prompts) % 200  # every call differs, like a real image model
        buffer = io.BytesIO()
        Image.new("RGB", (width, height), (shade, 99, 235)).save(buffer, "PNG")
        return buffer.getvalue()


_override: ImageGenerator | None = None


def set_image_generator(generator: ImageGenerator | None) -> None:
    global _override  # noqa: PLW0603
    _override = generator


def generator_for(workspace_id: uuid.UUID) -> ImageGenerator | None:
    """The workspace's image vendor, or ``None`` when no image key is connected."""
    if _override is not None:
        return _override
    connection = (
        Connection.objects.filter(workspace_id=workspace_id, provider=PROVIDER)
        .exclude(status__in=[ConnectionStatus.REVOKED, ConnectionStatus.PENDING])
        .first()
    )
    if connection is None:
        return None
    try:
        return OpenAiImageGenerator(read_secret(connection, CredentialKind.API_KEY))
    except NotFoundError:
        return None


# --- stored pictures --------------------------------------------------------------------------


def store_picture(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    data: bytes,
    *,
    name: str,
    alt_text: str,
    ai_generated: bool,
) -> MediaAsset:
    asset, _ = media_services.create_from_chunks(
        workspace_id,
        user_id,
        [data],
        filename=f"{name}.png",
        source=MediaSource.GENERATED,
        title=name,
    )
    asset.alt_text = alt_text[:500]
    asset.ai_generated = ai_generated
    asset.save(update_fields=["alt_text", "ai_generated", "updated_at"])
    return asset


def generate_picture(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    prompt: str,
    *,
    orientation: str,
    name: str,
    alt_text: str,
    crop: str | None,
) -> MediaAsset:
    """Generate one picture, store it and (optionally) crop it to an Instagram ratio."""
    generator = generator_for(workspace_id)
    if generator is None:
        raise ValidationFailedError("connect an image service first")
    data = generator.generate(prompt, orientation=orientation)
    return store_generated(workspace_id, user_id, data, name=name, alt_text=alt_text, crop=crop)


def store_generated(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    data: bytes,
    *,
    name: str,
    alt_text: str,
    crop: str | None,
) -> MediaAsset:
    """Store a generated picture and (optionally) crop it to a destination ratio."""
    asset = store_picture(
        workspace_id, user_id, data, name=name, alt_text=alt_text, ai_generated=True
    )
    if crop:
        cropped, _ = media_services.derive(asset, user_id, crop)
        cropped.alt_text = alt_text[:500]
        cropped.save(update_fields=["alt_text", "updated_at"])
        return cropped
    return asset


JPEG_QUALITIES = (92, 88, 84, 78, 70)
MAX_JPEG_BYTES = 8_000_000


def to_jpeg(asset: MediaAsset) -> MediaAsset:
    """A JPEG copy of an image (Instagram publishes JPEG); an existing copy is reused."""
    if asset.file_format == "JPEG":
        return asset
    storage = get_storage()
    with Image.open(storage.path(asset.storage_key)) as opened:
        flat = ImageOps.exif_transpose(opened).convert("RGB")
    data = b""
    for quality in JPEG_QUALITIES:
        buffer = io.BytesIO()
        flat.save(buffer, "JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= MAX_JPEG_BYTES:
            break
    key, sha, size = storage.save_stream(
        asset.workspace_id, [data], extension=".jpg", max_bytes=MAX_JPEG_BYTES * 2
    )
    existing = MediaAsset.objects.filter(workspace_id=asset.workspace_id, sha256=sha).first()
    if existing:
        storage.delete(key)
        return existing
    return MediaAsset.objects.create(
        workspace_id=asset.workspace_id,
        kind=MediaKind.IMAGE,
        source=MediaSource.DERIVED,
        name=f"{asset.name} (JPEG)"[:300],
        alt_text=asset.alt_text,
        storage_key=key,
        original_filename=f"{asset.pk}.jpg",
        mime_type="image/jpeg",
        file_format="JPEG",
        size_bytes=size,
        sha256=sha,
        width=flat.width,
        height=flat.height,
        ai_generated=asset.ai_generated,
        derived_from=asset,
        variant="instagram_jpeg",
        created_by_id=asset.created_by_id,
    )


# --- branded text cards -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Palette:
    background: str
    accent: str
    text: str


def palette_from(content: dict[str, Any] | None) -> Palette:
    """Brand colours from any brand-profile field that lists hex codes; a safe default otherwise."""
    found: list[str] = []
    for key, value in (content or {}).items():
        if "colour" in key.lower() or "color" in key.lower() or "palette" in key.lower():
            found.extend(HEX.findall(str(value)))
    colours = [*found, *DEFAULT_PALETTE[len(found) :]]
    background, accent = colours[0], colours[1]
    return Palette(background, accent, readable_on(background))


def _luminance(colour: str) -> float:
    red, green, blue = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4  # noqa: PLR2004

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast(first: str, second: str) -> float:
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def readable_on(background: str) -> str:
    """Black or white, whichever has the higher WCAG contrast on the background."""
    return (
        "#FFFFFF"
        if contrast("#FFFFFF", background) >= contrast("#000000", background)
        else "#000000"
    )


def _font(candidates: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    width: int,
) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def render_card(
    headline: str,
    body: str,
    *,
    palette: Palette,
    size: tuple[int, int] = CARD_SIZES["feed"],
    handle: str = "",
    position: str = "",
) -> bytes:
    """One text card as PNG bytes. Text stays inside a safe margin and keeps AA contrast."""
    width, height = size
    margin = round(width * 0.09)
    image = Image.new("RGB", size, palette.background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((margin, margin, margin + round(width * 0.14), margin + 10), fill=palette.accent)
    title_font = _font(FONT_CANDIDATES, round(width * 0.075))
    body_font = _font(BODY_FONT_CANDIDATES, round(width * 0.04))
    small_font = _font(BODY_FONT_CANDIDATES, round(width * 0.03))
    y = round(height * 0.22)
    for line in _wrap(draw, headline, title_font, width - 2 * margin)[:7]:
        draw.text((margin, y), line, font=title_font, fill=palette.text)
        y += round(width * 0.092)
    y += round(width * 0.04)
    for line in _wrap(draw, body, body_font, width - 2 * margin)[:8]:
        draw.text((margin, y), line, font=body_font, fill=palette.text)
        y += round(width * 0.055)
    footer_y = height - margin - round(width * 0.03)
    if handle:
        draw.text((margin, footer_y), handle, font=small_font, fill=palette.text)
    if position:
        draw.text(
            (width - margin - draw.textlength(position, font=small_font), footer_y),
            position,
            font=small_font,
            fill=palette.text,
        )
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def health_check(connection: Connection) -> HealthResult:
    try:
        key = read_secret(connection, CredentialKind.API_KEY)
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                f"{OPENAI_HOST}/models", headers={"Authorization": f"Bearer {key}"}
            )
        _raise_for(response)
    except AuthExpiredError:
        return HealthResult(ConnectionStatus.EXPIRED, "the API key was rejected")
    except DomainError as exc:
        return HealthResult(ConnectionStatus.DEGRADED, str(exc)[:200])
    return HealthResult(ConnectionStatus.ACTIVE)


def identify(api_key: str) -> tuple[str, str]:
    with httpx.Client(timeout=15.0) as client:
        response = client.get(
            f"{OPENAI_HOST}/models", headers={"Authorization": f"Bearer {api_key}"}
        )
    _raise_for(response)
    return "openai-images", "OpenAI image generation"


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=PROVIDER,
            display_name="OpenAI image generation",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="api_key",
                    label="OpenAI API key",
                    secret=True,
                    help_text="Used to generate post images. Text cards need no key.",
                ),
            ),
            identify=identify,
            health_check=health_check,
        )
    )
