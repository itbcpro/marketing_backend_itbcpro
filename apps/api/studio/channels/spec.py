"""What a channel accepts, as data. A new channel is one more registered ``ChannelSpec``."""

from dataclasses import dataclass, field

TEXT, IMAGE, CAROUSEL, VIDEO, STORY = "text", "image", "carousel", "video", "story"
PORTRAIT, SQUARE, LANDSCAPE, VERTICAL = "portrait", "square", "landscape", "vertical"


@dataclass(frozen=True, slots=True)
class KindSpec:
    """One kind of post on one channel and its limits.

    ``orientation`` is the picture shape this kind wants (portrait 4:5, square, landscape,
    vertical 9:16). ``card_size`` and ``crop`` name how a generated picture is sized.
    """

    kind: str
    max_chars: int
    hook_chars: int = 0
    min_media: int = 0
    max_media: int = 0
    needs_video: bool = False
    orientation: str = SQUARE
    card_size: str = "square"
    crop: str | None = None
    hashtags_max: int = 0
    hashtags_recommended: int = 0
    min_seconds: int = 0
    max_seconds: int = 0
    links_in_body: bool = True


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    key: str
    kinds: tuple[KindSpec, ...]
    handle_prefix: str = ""
    extras: dict[str, str] = field(default_factory=dict)

    def kind(self, name: str) -> KindSpec | None:
        return next((k for k in self.kinds if k.kind == name), None)

    @property
    def kind_names(self) -> list[str]:
        return [k.kind for k in self.kinds]
