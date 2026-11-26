"""Composition root: every registry of the studio is filled here, once, at startup."""

from studio.channels.builtin import register_builtin_channels
from studio.publishers.instagram import InstagramPublisher
from studio.publishers.ports import register_publisher
from studio.trends.community import HackerNewsSource, OwnSearchSource
from studio.trends.feeds import NewsSearchSource, RedditSource, RssSource, TrendingSearchSource
from studio.trends.ports import register_trend_source
from studio.visuals.ai_image import AiImageProvider
from studio.visuals.cards import TextCardProvider
from studio.visuals.clips import CardsVideoProvider
from studio.visuals.library import LibraryProvider
from studio.visuals.ports import register_picture_provider, register_video_provider


def register_all() -> None:
    register_builtin_channels()
    for source in (
        RssSource(),
        NewsSearchSource(),
        TrendingSearchSource(),
        RedditSource(),
        HackerNewsSource(),
        OwnSearchSource(),
    ):
        register_trend_source(source)
    for provider in (LibraryProvider(), AiImageProvider(), TextCardProvider()):
        register_picture_provider(provider)
    register_video_provider(CardsVideoProvider())
    register_publisher(InstagramPublisher())
