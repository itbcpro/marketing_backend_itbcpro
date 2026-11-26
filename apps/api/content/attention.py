"""What needs fixing first: the lowest-scoring pages, with the owner's ignored items set aside.

Scores come from ``checks``; an ignored check is dropped before scoring, so setting a check aside
raises the page's score instead of leaving a hole in it.
"""

from dataclasses import dataclass
from typing import Any

from content import checks, selectors
from content.models import ContentItem, IgnoredCheck, Website

DEFAULT_THRESHOLD = 70
BAD = ("fail", "warn")


@dataclass(frozen=True, slots=True)
class Row:
    item: ContentItem
    score: int
    problems: list[dict[str, Any]]
    ignored_checks: list[str]


def ignores_of(website: Website) -> tuple[set[str], dict[str, set[str]]]:
    """Pages ignored as a whole, and per page the checks ignored."""
    pages: set[str] = set()
    per_check: dict[str, set[str]] = {}
    for row in IgnoredCheck.objects.filter(website=website):
        if row.check_key:
            per_check.setdefault(row.item_key, set()).add(row.check_key)
        else:
            pages.add(row.item_key)
    return pages, per_check


def rate(item: ContentItem, ignored: set[str]) -> Row:
    results = checks.run_checks(item)
    kept = [c for c in results if c.key not in ignored]
    problems = [
        {"key": c.key, "status": c.status, "weight": c.weight}
        for c in sorted(kept, key=lambda c: (c.status != "fail", -c.weight))
        if c.status in BAD
    ]
    return Row(item, checks.score_of(kept), problems, sorted(ignored))


def worst(website: Website, threshold: int = DEFAULT_THRESHOLD) -> list[Row]:
    """Live pages scoring below ``threshold`` (lowest first), without ignored pages."""
    pages, per_check = ignores_of(website)
    rows = [
        rate(item, per_check.get(item.key, set()))
        for item in selectors.live_items(website)
        if item.key not in pages
    ]
    return sorted(
        (r for r in rows if r.score < threshold and r.problems),
        key=lambda r: (r.score, r.item.title),
    )
