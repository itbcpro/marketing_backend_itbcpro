"""Put a headline on a photo, the way people read posts: the picture says it, not the caption.

The photo is cropped to the post's shape, darkened at the bottom with a gradient so white text keeps
a strong contrast on any picture, and the headline is set large and bottom anchored inside the
platform's safe margins. The text is drawn here, not by an image model, so it is always exact.
"""

import io
import re

from PIL import Image, ImageDraw, ImageOps

from social import images

MARGIN = 0.08
SCRIM_START = 0.35
SCRIM_STRENGTH = 0.82
BASE_SHADE = 0.10
FIRST_SIZE = 0.105
LAST_SIZE = 0.056
SIZE_STEP = 0.006
MAX_LINES = 5
LINE_HEIGHT = 1.12
STORY_RESERVE = 0.21
STORY_RATIO = 1.6
MAX_WORDS = 10
MAX_CHARS = 80
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def short_headline(text: str) -> str:
    """The line that goes on the picture: the first sentence, cut to a few words."""
    first = SENTENCE.split(text.strip(), maxsplit=1)[0].strip()
    words = first.split()
    if len(first) <= MAX_CHARS and len(words) <= MAX_WORDS:
        return first.rstrip(".")
    return " ".join(words[:MAX_WORDS])[:MAX_CHARS].rstrip(" ,;:-")


def _scrim(size: tuple[int, int]) -> Image.Image:
    """A black layer that is clear at the top and dark at the bottom."""
    width, height = size
    column = Image.new("L", (1, height), 0)
    start = round(height * SCRIM_START)
    for y in range(height):
        if y >= start:
            fraction = (y - start) / max(height - start, 1)
            column.putpixel(
                (0, y), round(255 * (BASE_SHADE + (SCRIM_STRENGTH - BASE_SHADE) * fraction**0.8))
            )
        else:
            column.putpixel((0, y), round(255 * BASE_SHADE))
    return column.resize((width, height))


def _fit_text(
    draw: ImageDraw.ImageDraw, text: str, width: int, box_height: int
) -> tuple[list[str], int]:
    """The largest size at which the headline fits in the box, with its lines."""
    size = round(width * FIRST_SIZE)
    smallest = round(width * LAST_SIZE)
    step = max(round(width * SIZE_STEP), 1)
    while True:
        font = images._font(images.FONT_CANDIDATES, size)
        lines = images._wrap(draw, text, font, width)
        fits = len(lines) <= MAX_LINES and len(lines) * size * LINE_HEIGHT <= box_height
        if fits or size <= smallest:
            return lines[:MAX_LINES], size
        size -= step


def headline_on_photo(
    photo: bytes,
    headline: str,
    *,
    palette: images.Palette,
    size: tuple[int, int],
    handle: str = "",
) -> bytes:
    """The photo cropped to ``size`` with ``headline`` on it, as PNG bytes."""
    width, height = size
    with Image.open(io.BytesIO(photo)) as opened:
        base = ImageOps.fit(ImageOps.exif_transpose(opened).convert("RGB"), size)
    shade = _scrim(size)
    base = Image.composite(Image.new("RGB", size, (0, 0, 0)), base, shade)
    draw = ImageDraw.Draw(base)
    margin = round(width * MARGIN)
    is_story = height / width > STORY_RATIO
    bottom = height - margin - (round(height * STORY_RESERVE) if is_story else 0)
    footer = round(width * 0.035)
    if handle:
        small = images._font(images.BODY_FONT_CANDIDATES, footer)
        draw.text((margin, bottom - footer), handle, font=small, fill="#FFFFFF")
        bottom -= round(footer * 2.2)
    lines, text_size = _fit_text(draw, headline, width - 2 * margin, round(height * 0.5))
    font = images._font(images.FONT_CANDIDATES, text_size)
    block = round(len(lines) * text_size * LINE_HEIGHT)
    top = bottom - block
    draw.rectangle((margin, top - 26, margin + round(width * 0.14), top - 16), fill=palette.accent)
    for index, line in enumerate(lines):
        y = top + round(index * text_size * LINE_HEIGHT)
        draw.text((margin + 2, y + 2), line, font=font, fill="#000000")
        draw.text((margin, y), line, font=font, fill="#FFFFFF")
    buffer = io.BytesIO()
    base.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()
