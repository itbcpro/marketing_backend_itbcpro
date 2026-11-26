"""Feed based sources: any RSS or Atom feed, news searches, trending lists and discussion sites."""

import html
import math
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, field_validator

from core.errors import PermanentProviderError
from studio import net
from studio.trends.ports import RawTrend, rank_score

MAX_ITEMS = 40
SUMMARY_CHARS = 600
TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")
DIGITS = re.compile(r"[\d,.]+")
UNSAFE_XML = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)
TRAFFIC_REFERENCE = 1_000_000
MIN_SUB_LENGTH = 2


def _plain(text: str | None) -> str:
    return SPACE.sub(" ", html.unescape(TAG.sub(" ", text or ""))).strip()[:SUMMARY_CHARS]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except TypeError, ValueError:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _child_text(node: ET.Element, *names: str) -> str:
    for child in node:
        text = (child.text or "").strip()
        if _local(child.tag) in names and text:
            return text
    return ""


def _link(node: ET.Element) -> str:
    for child in node:
        if _local(child.tag) == "link":
            return (child.get("href") or child.text or "").strip()
    return ""


def _traffic(node: ET.Element) -> float:
    raw = _child_text(node, "approx_traffic")
    match = DIGITS.search(raw)
    if not match:
        return 0.0
    number = float(match.group(0).replace(",", "") or 0)
    return min(math.log10(number + 1) / math.log10(TRAFFIC_REFERENCE), 1.0)


def _related_titles(node: ET.Element) -> str:
    titles = [(el.text or "").strip() for el in node.iter() if _local(el.tag) == "news_item_title"]
    return "; ".join(t for t in titles if t)


def parse_feed(body: bytes) -> list[RawTrend]:
    """Items of an RSS 2.0, RSS 1.0 or Atom document (anything else is an error)."""
    if UNSAFE_XML.search(body[:4096]):
        raise PermanentProviderError("the feed uses unsupported XML", provider="feed")
    try:
        root = ET.fromstring(body)  # noqa: S314 - DOCTYPE and ENTITY refused above
    except ET.ParseError as exc:
        raise PermanentProviderError("the address is not a readable feed", provider="feed") from exc
    nodes = [n for n in root.iter() if _local(n.tag) in {"item", "entry"}][:MAX_ITEMS]
    items: list[RawTrend] = []
    for index, node in enumerate(nodes):
        title = _plain(_child_text(node, "title"))
        if not title:
            continue
        summary = _plain(_child_text(node, "description", "summary", "content")) or _related_titles(
            node
        )
        traffic = _traffic(node)
        items.append(
            RawTrend(
                title=title[:300],
                summary=summary,
                url=_link(node)[:1000],
                published_at=_date(_child_text(node, "pubdate", "published", "updated", "date")),
                score=max(traffic, rank_score(index, len(nodes))),
            )
        )
    return items


def _read(url: str) -> list[RawTrend]:
    response = net.fetch(url)
    if response.status_code >= 400:  # noqa: PLR2004 - HTTP error class
        raise PermanentProviderError(f"the feed answered {response.status_code}", provider="feed")
    return parse_feed(response.content)


class FeedParams(BaseModel):
    url: str = Field(min_length=8, max_length=1000)

    @field_validator("url")
    @classmethod
    def web_address(cls, value: str) -> str:
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("a web address is required")
        return value.strip()


class RssSource:
    kind: ClassVar[str] = "rss"
    params_model: ClassVar[type[BaseModel]] = FeedParams
    needs_network: ClassVar[bool] = True

    def default_name(self, params: BaseModel) -> str:
        return f"Feed {getattr(params, 'url', '')[:60]}"

    def fetch(self, workspace_id: uuid.UUID, params: Any) -> list[RawTrend]:
        return _read(params.url)


class NewsParams(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    country: str = Field(default="US", min_length=2, max_length=2)
    language: str = Field(default="en", min_length=2, max_length=5)
    when: str = Field(default="7d", pattern=r"^\d{1,2}[hdm]$")


class NewsSearchSource:
    """Headlines about a topic, from a public news search feed."""

    kind: ClassVar[str] = "news_search"
    params_model: ClassVar[type[BaseModel]] = NewsParams
    needs_network: ClassVar[bool] = True

    def default_name(self, params: BaseModel) -> str:
        return f"News: {getattr(params, 'query', '')}"

    def fetch(self, workspace_id: uuid.UUID, params: Any) -> list[RawTrend]:
        country = params.country.upper()
        query = quote_plus(f"{params.query} when:{params.when}")
        url = (
            "https://news.google.com/rss/search?"
            f"q={query}&hl={params.language}-{country}&gl={country}&ceid={country}:{params.language}"
        )
        return _read(url)


class TrendingParams(BaseModel):
    geo: str = Field(default="US", min_length=2, max_length=8)


class TrendingSearchSource:
    """What people are searching for right now in a country."""

    kind: ClassVar[str] = "trending_searches"
    params_model: ClassVar[type[BaseModel]] = TrendingParams
    needs_network: ClassVar[bool] = True

    def default_name(self, params: BaseModel) -> str:
        return f"Trending searches {getattr(params, 'geo', '')}"

    def fetch(self, workspace_id: uuid.UUID, params: Any) -> list[RawTrend]:
        return _read(f"https://trends.google.com/trending/rss?geo={quote_plus(params.geo.upper())}")


class RedditParams(BaseModel):
    subreddit: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9_]+$")
    period: str = Field(default="day", pattern=r"^(hour|day|week|month)$")


class RedditSource:
    """The best discussions of a community in a period."""

    kind: ClassVar[str] = "reddit"
    params_model: ClassVar[type[BaseModel]] = RedditParams
    needs_network: ClassVar[bool] = True

    def default_name(self, params: BaseModel) -> str:
        return f"Reddit r/{getattr(params, 'subreddit', '')}"

    def fetch(self, workspace_id: uuid.UUID, params: Any) -> list[RawTrend]:
        return _read(f"https://www.reddit.com/r/{params.subreddit}/top/.rss?t={params.period}")
