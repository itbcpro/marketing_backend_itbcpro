"""Let the image model design the whole post image, text included, then proofread it.

The model is the designer: it gets the exact words, the art direction and the brand colours, and
decides layout, hierarchy and typography itself. The risk is spelling, so Claude reads the result
back and the picture is drawn again (up to three tries, told what was wrong) until the text is
exact. When it never is, nothing is returned and the caller falls back to a plain photo with the
headline set by the studio, which is always exact.
"""

import dataclasses
import math
import time
from datetime import datetime
from typing import Any

from django.utils import timezone

from core.errors import RateLimitedError, RetryableProviderError
from llm import prompts
from media import services as media_services
from media.models import MediaAsset
from media.specs import SPEC_BY_KEY
from media.storage import get_storage
from social import images
from studio.channels.spec import CAROUSEL, IMAGE, LANDSCAPE, PORTRAIT, SQUARE, KindSpec
from studio.schemas import TextCheck
from studio.visuals import proofread
from studio.visuals.ports import VisualRequest, VisualResult

PROMPT_KEY = "studio/cover-design"
MAX_ATTEMPTS = 1
MAX_LINES = 9
BACKOFF_SECONDS = (20, 45, 90)
SAFETY = 0.04
RATIO_EPSILON = 0.01
ORIENTATION = {
    "portrait": "portrait",
    "square": "square",
    "landscape": "landscape",
    "vertical": "portrait",
}
SHAPE_WORDS = {
    "portrait": "Vertical 4:5",
    "square": "Square 1:1",
    "landscape": "Horizontal 1.91:1",
    "vertical": "Vertical 9:16",
}
DEFAULT_STYLE = "natural, unstyled, true to the place"
DEFAULT_DIRECTION = (
    "A modern, confident business post: strong typographic hierarchy, one clear hero element, "
    "generous spacing."
)


def orientation_for(kind: KindSpec) -> str:
    return ORIENTATION.get(kind.orientation, "square")


def shape_words(kind: KindSpec) -> str:
    return SHAPE_WORDS.get(kind.orientation, "Square 1:1")


def feed_shape(kind: KindSpec) -> KindSpec:
    """The shape for a designed feed picture: square, drawn whole and never cropped.

    Image models do not reliably keep text away from the edges, so trimming a tall or wide canvas
    to 4:5 or 1.91:1 cuts headlines off. Square is accepted by Instagram, Facebook and LinkedIn.
    """
    if kind.kind in {IMAGE, CAROUSEL} and kind.orientation in {PORTRAIT, LANDSCAPE}:
        return dataclasses.replace(kind, orientation=SQUARE, card_size="square", crop="meta_square")
    return kind


def safe_area(kind: KindSpec) -> str:
    """Tell the model how much of its canvas the final crop removes, so no text is cut off."""
    spec = SPEC_BY_KEY.get(kind.crop or "")
    size = images.SIZES[orientation_for(kind)]
    canvas_w, canvas_h = (int(v) for v in size.split("x"))
    target = spec.ratio[0] / spec.ratio[1] if spec and spec.ratio else canvas_w / canvas_h
    cut_x = cut_y = 0.0
    if canvas_w / canvas_h > target + RATIO_EPSILON:
        cut_x = (1 - (canvas_h * target) / canvas_w) / 2
    elif canvas_w / canvas_h < target - RATIO_EPSILON:
        cut_y = (1 - (canvas_w / target) / canvas_h) / 2
    if cut_x == 0 and cut_y == 0:
        return (
            "The whole picture is used as drawn, nothing is cropped. Keep every word at least "
            "10 percent away from each edge, and start the first line of text no higher than "
            "12 percent from the top."
        )
    side = math.ceil((cut_x + SAFETY) * 100)
    edge = math.ceil((cut_y + SAFETY) * 100)
    return (
        f"The picture is drawn on a {canvas_w}x{canvas_h} canvas and then cropped to the post "
        f"shape: the outer {math.ceil(cut_x * 100)} percent on the left and right and the outer "
        f"{math.ceil(cut_y * 100)} percent at the top and bottom are cut off. Keep every word and "
        f"every important element inside the remaining area: at least {side} percent from the "
        f"left and right edges and at least {edge} percent from the top and bottom edges. The "
        "first line of text must start no higher than that."
    )


def poster_lines(request: VisualRequest, headline: str) -> list[str]:
    """Every line of text the picture carries: the writer's poster text, else headline and line."""
    chosen = [line.strip() for line in request.brief.poster_lines if line.strip()]
    if chosen:
        return chosen[:MAX_LINES]
    return [line for line in (headline, request.brief.support_line.strip()) if line]


def _logo_note(request: VisualRequest) -> str:
    if not request.logo_corner:
        return ""
    corner = request.logo_corner.replace("-", " ")
    return (
        f"Leave the {corner} corner clean and empty (about 20 percent of the width and 12 percent "
        "of the height) with nothing important in it: the company logo is placed there "
        "afterwards. Do not draw any logo."
    )


def cover_prompt(request: VisualRequest, lines: list[str], slide_note: str = "") -> str:
    """The brief for a designed picture, rendered from the versioned template."""
    colours = ", ".join(c for c in (request.palette.accent, request.palette.background) if c)
    numbered = "\n".join(f'{number}. "{line}"' for number, line in enumerate(lines, start=1))
    return prompts.get_prompt(PROMPT_KEY).render(
        lines=numbered,
        direction=request.brief.design_direction.strip() or DEFAULT_DIRECTION,
        scene=request.brief.scene,
        shot=request.brief.shot,
        style=request.style.style.strip() or DEFAULT_STYLE,
        avoid=", ".join(request.style.avoid),
        orientation=shape_words(request.kind),
        palette=colours or "none",
        slide_note=slide_note,
        safe_area=safe_area(request.kind),
        logo_note=_logo_note(request),
    )


def _entry(
    prompt: str, verdict: TextCheck | None, number: int, expected: list[str]
) -> dict[str, Any]:
    """What was asked and what the proofreader found, kept so the owner can see why."""
    return {
        "kind": "designed",
        "attempt": number,
        "prompt": prompt,
        "expected": expected,
        "checked": verdict is not None,
        "ok": True if verdict is None else verdict.ok,
        "read_text": [] if verdict is None else verdict.read_text,
        "problems": [] if verdict is None else verdict.problems,
    }


def _drop(asset: MediaAsset, since: datetime) -> None:
    """Delete a rejected attempt. Only files made by this attempt: media de-duplication may hand
    back an older picture with identical bytes, and that one may already be in use."""
    original = asset.derived_from
    if asset.created_at >= since:
        media_services.delete(asset)
    if original is not None and original.created_at >= since:
        media_services.delete(original)


def _draw(generator: images.ImageGenerator, prompt: str, orientation: str) -> bytes:
    """Draw one picture, waiting and trying again when the image service says to slow down."""
    for pause in (*BACKOFF_SECONDS, None):
        try:
            return generator.generate(prompt, orientation=orientation)
        except RateLimitedError, RetryableProviderError:
            if pause is None:
                raise
            time.sleep(pause)
    raise AssertionError("unreachable")


def design(
    request: VisualRequest,
    lines: list[str],
    slide_note: str = "",
    trace: list[dict[str, Any]] | None = None,
) -> VisualResult | None:
    """A proofread designed picture, or ``None`` when the image model never got the text right."""
    generator = images.generator_for(request.workspace_id)
    if generator is None or not any(line.strip() for line in lines):
        return None
    began = timezone.now()
    problems = ""
    for _attempt in range(MAX_ATTEMPTS):
        # Every try keeps the designed look and every line: a retry only tells the model what it
        # got wrong, so the picture does not degrade into a plain poster after a stray word.
        expected = lines
        prompt = cover_prompt(request, expected, slide_note)
        if problems:
            prompt += (
                f"\n\nThe previous attempt had text errors: {problems}. Draw it again with the "
                "text exactly as written and nothing else written."
            )
        data = _draw(generator, prompt, orientation_for(request.kind))
        asset = images.store_generated(
            request.workspace_id,
            request.user_id,
            data,
            name=(request.title or "Cover")[:80],
            alt_text=request.brief.alt_text,
            crop=request.kind.crop,
        )
        verdict = proofread.check(
            request.workspace_id, get_storage().path(asset.storage_key).read_bytes(), expected
        )
        if trace is not None:
            trace.append(_entry(prompt, verdict, len(trace) + 1, expected))
        if verdict is None or verdict.ok:
            media = [{"asset_id": str(asset.pk), "alt_text": request.brief.alt_text}]
            return VisualResult(media=media, has_text=True)
        _drop(asset, began)
        problems = "; ".join(verdict.problems) or "the text did not match"
    return None
