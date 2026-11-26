"""Pictures from an image service: a finished designed cover, or a plain art-directed photograph."""

import dataclasses
import uuid
from typing import Any, ClassVar

from llm import prompts
from social import images
from studio.channels.spec import IMAGE, STORY, KindSpec
from studio.visuals import designer, headline
from studio.visuals.ports import VisualRequest, VisualResult

PROMPT_KEY = "studio/image-direction"


def direction(request: VisualRequest) -> str:
    """The prompt for a plain photograph, rendered from the versioned template."""
    colours = ", ".join(c for c in (request.palette.background, request.palette.accent) if c)
    return prompts.get_prompt(PROMPT_KEY).render(
        scene=request.brief.scene,
        shot=request.brief.shot,
        style=request.style.style.strip() or designer.DEFAULT_STYLE,
        avoid=", ".join(request.style.avoid),
        orientation=designer.shape_words(request.kind),
        palette=colours or "none",
    )


class AiImageProvider:
    key: ClassVar[str] = "ai_image"

    def supports(self, kind: KindSpec) -> bool:
        return kind.kind in {IMAGE, STORY}

    def available(self, workspace_id: uuid.UUID) -> bool:
        return images.generator_for(workspace_id) is not None

    def make(self, request: VisualRequest) -> VisualResult:
        trace: list[dict[str, Any]] = []
        if request.style.cover_style == "designed":
            lines = designer.poster_lines(request, headline.text_for(request))
            designed = designer.design(request, lines, trace=trace)
            if designed is not None:
                return dataclasses.replace(designed, trace=tuple(trace))
            # The model never spelled it right: a plain photo; the studio then sets the headline.
        prompt = direction(request)
        trace.append({"kind": "plain", "prompt": prompt})
        asset = images.generate_picture(
            request.workspace_id,
            request.user_id,
            prompt,
            orientation=designer.orientation_for(request.kind),
            name=(request.title or "Picture")[:80],
            alt_text=request.brief.alt_text,
            crop=request.kind.crop,
        )
        media = [{"asset_id": str(asset.pk), "alt_text": request.brief.alt_text}]
        return VisualResult(media=media, trace=tuple(trace))
