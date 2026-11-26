"""Weights of the audit score. The total is computed here, never taken from the model."""

from content.blog.schemas import CategoryScore

WEIGHTS = {
    "search_intent": 10,
    "content_depth": 15,
    "on_page_seo": 10,
    "semantic_coverage": 10,
    "eeat": 10,
    "aeo_snippets": 10,
    "geo_citation": 10,
    "internal_linking": 5,
    "external_sources": 5,
    "technical_recommendations": 5,
    "readability_ux": 5,
    "conversion": 5,
}


def total(scores: list[CategoryScore], failed_checks: list[str]) -> int:
    """Weighted score; a category the model skipped counts as zero; failed hard checks cap it."""
    by_key = {s.category: s.score for s in scores}
    value = sum(by_key.get(key, 0) * weight for key, weight in WEIGHTS.items()) / sum(
        WEIGHTS.values()
    )
    cap = 100 - 10 * len(failed_checks)
    return max(0, min(round(value), cap))
