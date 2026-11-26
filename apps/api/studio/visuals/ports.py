"""Ports for making pictures and videos. Providers register themselves; the director picks.

A picture provider may decline a request (``supports``) or be unusable right now (``available``);
the director then tries the next one in the owner's order of preference. A provider raises a
``DomainError`` when it tried and failed, which the director records without losing the text.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from social.images import Palette
from social.schemas import ReelPlan, Slide
from studio.channels.spec import KindSpec
from studio.config import VisualConfig
from studio.schemas import VisualBrief


@dataclass(frozen=True, slots=True)
class VisualRequest:
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    channel: str
    kind: KindSpec
    title: str
    headline: str
    slides: list[Slide]
    brief: VisualBrief
    palette: Palette
    style: VisualConfig
    handle: str = ""
    logo_corner: str = ""
    brand_id: uuid.UUID | None = None
    recently_used: frozenset[uuid.UUID] = frozenset()


@dataclass(frozen=True, slots=True)
class VisualResult:
    media: list[dict[str, Any]] = field(default_factory=list)
    cover_asset_id: uuid.UUID | None = None
    source: str = ""
    has_text: bool = False
    trace: tuple[dict[str, Any], ...] = ()


class PictureProvider(Protocol):
    key: ClassVar[str]

    def supports(self, kind: KindSpec) -> bool: ...

    def available(self, workspace_id: uuid.UUID) -> bool: ...

    def make(self, request: VisualRequest) -> VisualResult: ...


class VideoProvider(Protocol):
    key: ClassVar[str]

    def available(self, workspace_id: uuid.UUID) -> bool: ...

    def make(
        self, request: VisualRequest, plan: ReelPlan, *, previous: uuid.UUID | None
    ) -> uuid.UUID: ...


_PICTURES: dict[str, PictureProvider] = {}
_VIDEOS: dict[str, VideoProvider] = {}
DEFAULT_VIDEO = "cards"


def register_picture_provider(provider: PictureProvider) -> None:
    _PICTURES[provider.key] = provider


def register_video_provider(provider: VideoProvider) -> None:
    _VIDEOS[provider.key] = provider


def picture_provider(key: str) -> PictureProvider | None:
    return _PICTURES.get(key)


def video_provider(key: str) -> VideoProvider | None:
    return _VIDEOS.get(key) or _VIDEOS.get(DEFAULT_VIDEO)


def picture_provider_keys() -> list[str]:
    return list(_PICTURES)


def video_provider_keys() -> list[str]:
    return list(_VIDEOS)
