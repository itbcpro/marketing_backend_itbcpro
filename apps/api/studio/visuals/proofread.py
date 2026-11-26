"""Read a designed picture back and confirm its text is exactly what was asked for.

Image models draw words, and they sometimes misspell them. Claude looks at the finished picture and
compares it with the requested lines. A check that cannot run (no model, an outage) never blocks a
picture: it is accepted and the post still shows its "made by AI" warning.
"""

import base64
import io
import json
import uuid

from PIL import Image

from core.errors import DomainError
from core.logging import get_logger
from llm import prompts
from llm.ports import LlmRequest, Message, OpaqueBlock, SystemBlock, TextBlock
from llm.service import get_llm_service
from llm.structured import complete_structured
from studio.schemas import TextCheck

log = get_logger(__name__)

PROMPT_KEY = "studio/text-check"
LONGEST_SIDE = 1280
JPEG_QUALITY = 85


def _shrink(data: bytes) -> bytes:
    """A smaller JPEG of the picture: enough to read the text, cheap to send."""
    with Image.open(io.BytesIO(data)) as opened:
        picture = opened.convert("RGB")
    picture.thumbnail((LONGEST_SIDE, LONGEST_SIDE))
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def _image_block(data: bytes) -> OpaqueBlock:
    encoded = base64.b64encode(_shrink(data)).decode()
    return OpaqueBlock(
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": encoded}}
    )


def check(workspace_id: uuid.UUID, picture: bytes, expected: list[str]) -> TextCheck | None:
    """The proofreader's verdict, or ``None`` when it could not be asked."""
    prompt = prompts.get_prompt(PROMPT_KEY)
    system = prompt.render(expected=json.dumps(expected, ensure_ascii=False))
    request = LlmRequest(
        tier=prompt.tier,
        system=[SystemBlock(system)],
        messages=[Message("user", [_image_block(picture), TextBlock("Proofread this picture.")])],
        max_tokens=1500,
    )
    try:
        verdict, _ = complete_structured(
            get_llm_service(workspace_id),
            request,
            TextCheck,
            workspace_id=workspace_id,
            prompt_key=f"{PROMPT_KEY}@v{prompt.version}",
        )
    except DomainError as exc:
        log.warning("studio_text_check_unavailable", code=exc.code.value)
        return None
    return verdict
