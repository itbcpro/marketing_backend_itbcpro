"""On-page checks that need no AI: fixed rules over what the inventory already knows.

Each check returns ``pass``, ``warn``, ``fail`` or ``na`` with the numbers behind it, so the page
dashboard can show why. The score weighs failures more than warnings.
"""

import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from content.models import ContentItem, ItemSource

TITLE_MIN, TITLE_MAX = 30, 60
DESC_MIN, DESC_MAX = 70, 160
POST_WORDS_GOOD, POST_WORDS_MIN = 800, 400
PAGE_WORDS_MIN = 250
HTTP_ERROR = 400
MIN_H2 = 2
GOOD_INTERNAL_LINKS = 3
SLUG_MAX = 75
SENTENCE_WORDS_MAX = 24
MIN_WORDS_FOR_STRUCTURE = 500
SENTENCE_END = re.compile(r"[.!?]+\s")
WEIGHTS = {"fail": 0.0, "warn": 0.5, "pass": 1.0}


@dataclass(frozen=True, slots=True)
class Check:
    key: str
    status: str
    weight: int
    values: dict[str, Any]


def _length_check(key: str, text: str, low: int, high: int, weight: int) -> Check:
    size = len(text.strip())
    if size == 0:
        status = "fail"
    elif size < low or size > high:
        status = "warn"
    else:
        status = "pass"
    return Check(key, status, weight, {"length": size, "min": low, "max": high})


def _words_check(item: ContentItem, words: int) -> Check:
    if item.kind == "post":
        status = (
            "pass" if words >= POST_WORDS_GOOD else "warn" if words >= POST_WORDS_MIN else "fail"
        )
        floor = POST_WORDS_GOOD
    else:
        status = "pass" if words >= PAGE_WORDS_MIN else "warn"
        floor = PAGE_WORDS_MIN
    return Check("word_count", status, 10, {"words": words, "target": floor})


def _structure_check(item: ContentItem, words: int) -> Check:
    levels = [int(h["level"]) for h in item.outline]
    h2 = levels.count(2)
    skipped = any(b - a > 1 for a, b in pairwise(levels))
    if words < MIN_WORDS_FOR_STRUCTURE:
        return Check("headings", "na", 8, {"h2": h2})
    status = "fail" if h2 == 0 else "warn" if (h2 < MIN_H2 or skipped) else "pass"
    return Check("headings", status, 8, {"h2": h2, "skipped_levels": skipped})


def _h1_check(item: ContentItem) -> Check:
    if item.source == ItemSource.WORDPRESS:
        return Check("h1", "na", 8, {})
    count = int(item.stats.get("h1_count", 0))
    return Check("h1", "pass" if count == 1 else "fail", 8, {"count": count})


def _links_check(item: ContentItem) -> Check:
    internal = int(item.stats.get("internal_links", 0))
    if item.kind != "post":
        return Check("internal_links", "na", 8, {"count": internal})
    status = "pass" if internal >= GOOD_INTERNAL_LINKS else "warn" if internal >= 1 else "fail"
    return Check("internal_links", status, 8, {"count": internal, "target": GOOD_INTERNAL_LINKS})


def _images_check(item: ContentItem) -> Check:
    images = int(item.stats.get("images", 0))
    missing = int(item.stats.get("images_missing_alt", 0))
    if images == 0:
        return Check("image_alt", "na", 6, {"images": 0})
    return Check(
        "image_alt", "pass" if missing == 0 else "warn", 6, {"images": images, "missing": missing}
    )


def _readability_check(text: str) -> Check:
    words = len(text.split())
    sentences = max(1, len(SENTENCE_END.findall(text + " ")))
    average = round(words / sentences, 1)
    if words < PAGE_WORDS_MIN:
        return Check("readability", "na", 5, {"average_sentence_words": average})
    status = "pass" if average <= SENTENCE_WORDS_MAX else "warn"
    return Check("readability", status, 5, {"average_sentence_words": average})


def _slug_check(item: ContentItem) -> Check:
    slug = item.slug
    bad = bool(slug) and (len(slug) > SLUG_MAX or slug != slug.lower() or "_" in slug)
    return Check("slug", "na" if not slug else "warn" if bad else "pass", 4, {"length": len(slug)})


def run_checks(item: ContentItem) -> list[Check]:
    stats = item.stats or {}
    seo = item.seo or {}
    words = int(stats.get("word_count", 0))
    seo_title = str(seo.get("title") or item.title)
    robots = str(seo.get("robots", "")).lower()
    canonical = str(seo.get("canonical", ""))
    checks = [
        _length_check("seo_title", seo_title, TITLE_MIN, TITLE_MAX, 14),
        _length_check("meta_description", str(seo.get("description", "")), DESC_MIN, DESC_MAX, 14),
        _h1_check(item),
        _structure_check(item, words),
        _words_check(item, words),
        _links_check(item),
        _images_check(item),
        _readability_check(item.text),
        _slug_check(item),
        Check("indexable", "fail" if "noindex" in robots else "pass", 10, {"robots": robots}),
        Check(
            "canonical",
            "na"
            if not canonical
            else "pass"
            if canonical.rstrip("/") == item.url.rstrip("/")
            else "warn",
            4,
            {"canonical": canonical},
        ),
        Check(
            "in_sitemap",
            "na" if item.status != "publish" else "pass" if item.in_sitemap else "warn",
            4,
            {},
        ),
    ]
    if item.http_status is not None:
        checks.append(
            Check(
                "http_status",
                "pass" if item.http_status < HTTP_ERROR else "fail",
                10,
                {"status": item.http_status},
            )
        )
    return checks


def score_of(checks: list[Check]) -> int:
    """0-100: share of the weight earned by checks that apply."""
    applicable = [c for c in checks if c.status != "na"]
    total = sum(c.weight for c in applicable)
    if total == 0:
        return 0
    return round(100 * sum(c.weight * WEIGHTS[c.status] for c in applicable) / total)


def as_dicts(checks: list[Check]) -> list[dict[str, Any]]:
    return [
        {"key": c.key, "status": c.status, "weight": c.weight, "values": c.values} for c in checks
    ]
