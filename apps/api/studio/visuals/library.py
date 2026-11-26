"""Pictures from the company's own media library: real photos beat any generated one."""

import re
import uuid
from typing import ClassVar

from core.errors import NotFoundError
from media import services as media_services
from media.models import MediaAsset, MediaKind, MediaSource
from studio.channels.spec import IMAGE, STORY, KindSpec
from studio.visuals.ports import VisualRequest, VisualResult

WORD = re.compile(r"[a-z0-9]{3,}")
MAX_CANDIDATES = 400
SKIP_SOURCES = (MediaSource.GENERATED, MediaSource.DERIVED)
MAX_SCENE_WORDS = 30


def _tokens(*texts: str) -> set[str]:
    return {w for text in texts for w in WORD.findall(text.lower())}


def _usable(workspace_id: uuid.UUID) -> list[MediaAsset]:
    rows = (
        MediaAsset.objects.filter(
            workspace_id=workspace_id, kind=MediaKind.IMAGE, ai_generated=False
        )
        .exclude(source__in=SKIP_SOURCES)
        .exclude(storage_key="")
    )
    return list(rows[:MAX_CANDIDATES])


def _score(asset: MediaAsset, wanted: set[str], keywords: set[str]) -> float:
    have = _tokens(asset.name, asset.description, asset.alt_text, " ".join(map(str, asset.tags)))
    return len(have & keywords) * 2 + len(have & wanted)


class LibraryProvider:
    key: ClassVar[str] = "library"

    def supports(self, kind: KindSpec) -> bool:
        return kind.kind in {IMAGE, STORY}

    def available(self, workspace_id: uuid.UUID) -> bool:
        return bool(_usable(workspace_id))

    def make(self, request: VisualRequest) -> VisualResult:
        keywords = _tokens(*request.brief.library_keywords)
        wanted = _tokens(request.brief.scene, request.title) - keywords
        scored = [
            (_score(a, wanted, keywords), a)
            for a in _usable(request.workspace_id)
            if a.pk not in request.recently_used
        ]
        scored = [pair for pair in scored if pair[0] > 0]
        if not scored:
            raise NotFoundError("no matching picture in the media library")
        best = max(scored, key=lambda pair: pair[0])[1]
        picked = (
            media_services.derive(best, request.user_id, request.kind.crop)[0]
            if request.kind.crop
            else best
        )
        alt = best.alt_text or request.brief.alt_text
        return VisualResult(media=[{"asset_id": str(picked.pk), "alt_text": alt}])
