"""Specs of the channels the studio knows today (platform facts, not user settings)."""

from studio.channels.registry import register_channel
from studio.channels.spec import (
    CAROUSEL,
    IMAGE,
    LANDSCAPE,
    PORTRAIT,
    SQUARE,
    STORY,
    TEXT,
    VERTICAL,
    VIDEO,
    ChannelSpec,
    KindSpec,
)

INSTAGRAM = ChannelSpec(
    key="instagram",
    handle_prefix="@",
    kinds=(
        KindSpec(IMAGE, 2200, 125, 1, 1, orientation=PORTRAIT, card_size="feed",
                 crop="meta_portrait", hashtags_max=30, hashtags_recommended=8),
        KindSpec(CAROUSEL, 2200, 125, 2, 10, orientation=PORTRAIT, card_size="feed",
                 crop="meta_portrait", hashtags_max=30, hashtags_recommended=8),
        KindSpec(VIDEO, 2200, 125, 1, 1, needs_video=True, orientation=VERTICAL,
                 card_size="story", crop="meta_story", hashtags_max=30, hashtags_recommended=5,
                 min_seconds=3, max_seconds=90),
        KindSpec(STORY, 0, 0, 1, 1, orientation=VERTICAL, card_size="story", crop="meta_story"),
    ),
    extras={"video_label": "reel"},
)  # fmt: skip

FACEBOOK = ChannelSpec(
    key="facebook",
    kinds=(
        KindSpec(TEXT, 63000, 140, hashtags_max=10, hashtags_recommended=3),
        KindSpec(IMAGE, 63000, 140, 1, 1, orientation=SQUARE, card_size="square",
                 crop="meta_square", hashtags_max=10, hashtags_recommended=3),
        KindSpec(CAROUSEL, 63000, 140, 2, 10, orientation=SQUARE, card_size="square",
                 crop="meta_square", hashtags_max=10, hashtags_recommended=3),
        KindSpec(VIDEO, 63000, 140, 1, 1, needs_video=True, orientation=VERTICAL,
                 card_size="story", crop="meta_story", hashtags_max=10, hashtags_recommended=3,
                 min_seconds=3, max_seconds=90),
        KindSpec(STORY, 0, 0, 1, 1, orientation=VERTICAL, card_size="story", crop="meta_story"),
    ),
    extras={"video_label": "reel"},
)  # fmt: skip

LINKEDIN = ChannelSpec(
    key="linkedin",
    kinds=(
        KindSpec(TEXT, 3000, 210, hashtags_max=5, hashtags_recommended=3),
        KindSpec(IMAGE, 3000, 210, 1, 1, orientation=LANDSCAPE, card_size="square",
                 crop="meta_landscape", hashtags_max=5, hashtags_recommended=3),
        KindSpec(CAROUSEL, 3000, 210, 2, 9, orientation=SQUARE, card_size="square",
                 crop="meta_square", hashtags_max=5, hashtags_recommended=3),
        KindSpec(VIDEO, 3000, 210, 1, 1, needs_video=True, orientation=VERTICAL,
                 card_size="story", crop="meta_story", hashtags_max=5, hashtags_recommended=3,
                 min_seconds=3, max_seconds=600),
    ),
)  # fmt: skip

BUILTIN = (INSTAGRAM, FACEBOOK, LINKEDIN)


def register_builtin_channels() -> None:
    for spec in BUILTIN:
        register_channel(spec)
