"""Sources that are not feeds: Hacker News front page and the company's own rising searches."""

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from django.db.models import Sum
from django.utils import timezone
from pydantic import BaseModel, Field

from content.models import GscQueryDaily
from core.errors import PermanentProviderError
from studio import net
from studio.trends.ports import RawTrend

HN_URL = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage={count}"
HN_ITEM = "https://news.ycombinator.com/item?id={id}"
HN_POINTS_REFERENCE = 500
MAX_QUERIES = 400
TOP_QUERIES = 15
MIN_IMPRESSIONS = 20
GROWTH_REFERENCE = 3.0


class HackerNewsParams(BaseModel):
    count: int = Field(default=20, ge=5, le=50)
    min_points: int = Field(default=50, ge=0, le=5000)


class HackerNewsSource:
    """Technology stories people are voting up right now."""

    kind: ClassVar[str] = "hacker_news"
    params_model: ClassVar[type[BaseModel]] = HackerNewsParams
    needs_network: ClassVar[bool] = True

    def default_name(self, params: BaseModel) -> str:
        return "Hacker News front page"

    def fetch(self, workspace_id: uuid.UUID, params: Any) -> list[RawTrend]:
        response = net.fetch(HN_URL.format(count=params.count))
        if response.status_code >= 400:  # noqa: PLR2004 - HTTP error class
            raise PermanentProviderError("Hacker News did not answer", provider="hacker_news")
        hits = json.loads(response.content).get("hits", [])
        items: list[RawTrend] = []
        for hit in hits:
            points = int(hit.get("points") or 0)
            title = str(hit.get("title") or "").strip()
            if not title or points < params.min_points:
                continue
            created = hit.get("created_at_i")
            items.append(
                RawTrend(
                    title=title[:300],
                    summary=f"{points} points, {int(hit.get('num_comments') or 0)} comments",
                    url=str(hit.get("url") or HN_ITEM.format(id=hit.get("objectID", ""))),
                    published_at=datetime.fromtimestamp(created, UTC) if created else None,
                    score=min(points / HN_POINTS_REFERENCE, 1.0),
                )
            )
        return items


class OwnSearchParams(BaseModel):
    days: int = Field(default=14, ge=7, le=60)


class OwnSearchSource:
    """Searches that bring people to the company's own site and are growing."""

    kind: ClassVar[str] = "own_searches"
    params_model: ClassVar[type[BaseModel]] = OwnSearchParams
    needs_network: ClassVar[bool] = False

    def default_name(self, params: BaseModel) -> str:
        return "Rising searches on our site"

    def _totals(self, workspace_id: uuid.UUID, start: Any, end: Any) -> dict[str, tuple[int, int]]:
        rows = (
            GscQueryDaily.objects.filter(workspace_id=workspace_id, date__gte=start, date__lt=end)
            .values("query")
            .annotate(impressions=Sum("impressions"), clicks=Sum("clicks"))
            .order_by("-impressions")[:MAX_QUERIES]
        )
        return {r["query"]: (int(r["impressions"] or 0), int(r["clicks"] or 0)) for r in rows}

    def fetch(self, workspace_id: uuid.UUID, params: Any) -> list[RawTrend]:
        today = timezone.localdate()
        recent_from = today - timedelta(days=params.days)
        before_from = recent_from - timedelta(days=params.days)
        recent = self._totals(workspace_id, recent_from, today + timedelta(days=1))
        before = self._totals(workspace_id, before_from, recent_from)
        rows: list[tuple[float, str, int, int, int]] = []
        for query, (impressions, clicks) in recent.items():
            if impressions < MIN_IMPRESSIONS:
                continue
            was = before.get(query, (0, 0))[0]
            growth = (impressions - was) / max(was, MIN_IMPRESSIONS)
            rows.append((growth * math.log10(impressions + 1), query, impressions, clicks, was))
        rows.sort(reverse=True)
        return [
            RawTrend(
                title=query,
                summary=(
                    f"{impressions} search appearances in the last {params.days} days "
                    f"(before: {was}), {clicks} clicks"
                ),
                score=max(min(value / GROWTH_REFERENCE, 1.0), 0.0),
            )
            for value, query, impressions, clicks, was in rows[:TOP_QUERIES]
            if value > 0
        ]
