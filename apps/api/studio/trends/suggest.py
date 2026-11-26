"""Suggest trend sources from what the owner told us (topics, country). Nothing is created here."""

from dataclasses import dataclass, field
from typing import Any

from studio.config import TrendConfig
from studio.trends.ports import get_trend_source

MAX_TOPIC_SOURCES = 8
HAS_COUNTRY = 2


@dataclass(frozen=True, slots=True)
class SuggestedSource:
    kind: str
    name: str
    params: dict[str, Any] = field(default_factory=dict)


def _country(geo: str) -> str:
    return geo.upper() if len(geo) == HAS_COUNTRY else "US"


def suggestions(config: TrendConfig) -> list[SuggestedSource]:
    country = _country(config.geo)
    picks = [
        SuggestedSource("news_search", "", {"query": topic, "country": country})
        for topic in config.topics[:MAX_TOPIC_SOURCES]
    ]
    picks.append(SuggestedSource("trending_searches", "", {"geo": country}))
    picks.append(SuggestedSource("own_searches", "", {}))
    named: list[SuggestedSource] = []
    for pick in picks:
        kind = get_trend_source(pick.kind)
        model = kind.params_model.model_validate(pick.params)
        named.append(SuggestedSource(pick.kind, kind.default_name(model), model.model_dump()))
    return named
