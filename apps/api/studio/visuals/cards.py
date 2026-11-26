"""Branded text cards drawn locally: exact words, brand colours, always readable."""

import uuid
from typing import ClassVar

from social import images
from studio.channels.spec import CAROUSEL, IMAGE, STORY, KindSpec
from studio.visuals.ports import VisualRequest, VisualResult

TITLE_CHARS = 60


class TextCardProvider:
    key: ClassVar[str] = "text_card"

    def supports(self, kind: KindSpec) -> bool:
        return kind.kind in {IMAGE, CAROUSEL, STORY}

    def available(self, workspace_id: uuid.UUID) -> bool:
        return True

    def _store(self, request: VisualRequest, data: bytes, name: str, alt: str) -> dict[str, str]:
        asset = images.store_picture(
            request.workspace_id,
            request.user_id,
            data,
            name=name[:80],
            alt_text=alt,
            ai_generated=False,
        )
        return {"asset_id": str(asset.pk), "alt_text": alt}

    def make(self, request: VisualRequest) -> VisualResult:
        size = images.CARD_SIZES.get(request.kind.card_size, images.CARD_SIZES["square"])
        if request.kind.kind == CAROUSEL and request.slides:
            return VisualResult(media=self._slides(request, size), has_text=True)
        text = request.brief.on_image_text or request.headline
        data = images.render_card(
            text, "", palette=request.palette, size=size, handle=request.handle
        )
        alt = request.brief.alt_text or text
        return VisualResult(
            media=[self._store(request, data, request.title or text, alt)], has_text=True
        )

    def _slides(self, request: VisualRequest, size: tuple[int, int]) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        total = len(request.slides)
        for number, slide in enumerate(request.slides, start=1):
            data = images.render_card(
                slide.headline,
                slide.body,
                palette=request.palette,
                size=size,
                handle=request.handle,
                position=f"{number}/{total}",
            )
            alt = slide.alt_text or slide.headline
            name = f"{request.title[:TITLE_CHARS]} slide {number}"
            entries.append(self._store(request, data, name, alt))
        return entries
