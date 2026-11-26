"""AI page review: analyse one page, turn the answer into suggestions, rewrite single fields."""

import re
import uuid
from typing import Any

from django.utils import timezone

from content import ai, brief, checks, search, seo_meta
from content.models import (
    AnalysisStatus,
    ContentItem,
    PageAnalysis,
    PageSuggestion,
    SuggestionStatus,
    Website,
)
from core.errors import DomainError, ValidationFailedError
from core.logging import get_logger

log = get_logger(__name__)

PAGE_TEXT_CHARS = 12_000
REWRITE_TEXT_CHARS = 3_000
NEIGHBORS = 25
WORD = re.compile(r"[a-z0-9]{3,}")
FIELD_RULES = {
    "seo_title": "About 50 to 60 characters (never over 65). Primary keyword near the front.",
    "meta_description": "About 140 to 158 characters (never over 170). Says what the visitor gets.",
    "title": "The visible page heading. Clear and specific, under 80 characters.",
    "slug": "Lower case, words joined by hyphens, under 60 characters, no dates.",
}


def current_value(item: ContentItem, field: str) -> str:
    seo = item.seo or {}
    return {
        "seo_title": str(seo.get("title") or item.title),
        "meta_description": str(seo.get("description", "")),
        "title": item.title,
        "slug": item.slug,
    }.get(field, "")


def page_view(item: ContentItem, text_chars: int = PAGE_TEXT_CHARS) -> dict[str, Any]:
    return {
        "title": item.title,
        "url": item.url,
        "kind": item.kind,
        "status": item.status,
        "language": item.language,
        "seo": item.seo,
        "headings": item.outline,
        "counts": item.stats,
        "categories_and_tags": item.terms,
        "text": item.text[:text_chars],
    }


def site_view(website: Website) -> dict[str, Any]:
    profile = website.profile or {}
    return {
        "name": website.name,
        "address": website.base_url,
        "kind": website.kind,
        "language": (profile.get("settings") or {}).get("language", ""),
        "detected": profile.get("detected", {}),
    }


def neighbors_of(item: ContentItem, limit: int = NEIGHBORS) -> list[dict[str, str]]:
    """Other live pages worth linking to, ranked by words shared with this page."""
    wanted = set(WORD.findall(" ".join([item.title, *[h["text"] for h in item.outline]]).lower()))
    rows = (
        ContentItem.objects.filter(website=item.website, removed_at__isnull=True, status="publish")
        .exclude(pk=item.pk)
        .exclude(kind="other")
        .exclude(url="")
    )
    scored = sorted(
        rows,
        key=lambda o: (
            -len(wanted & set(WORD.findall(o.title.lower()))),
            -(o.stats or {}).get("word_count", 0),
        ),
    )
    return [{"title": o.title, "url": o.url, "kind": o.kind} for o in scored[:limit]]


def start_analysis(item: ContentItem, *, requested_by: str, instruction: str = "") -> PageAnalysis:
    return PageAnalysis.objects.create(
        workspace_id=item.workspace_id,
        item=item,
        instruction=instruction[:500],
        requested_by=requested_by,
        content_hash=item.content_hash,
    )


def _clean_review(review: ai.PageReview, neighbor_urls: set[str]) -> dict[str, Any]:
    data = review.model_dump(mode="json")
    data["internal_links"] = [
        link for link in data["internal_links"] if link["target_url"] in neighbor_urls
    ]
    return data


def _store_suggestions(analysis: PageAnalysis, data: dict[str, Any]) -> None:
    item = analysis.item
    PageSuggestion.objects.filter(item=item, status=SuggestionStatus.PROPOSED).delete()
    seen: set[str] = set()
    for proposal in data["proposals"]:
        field = proposal["field"]
        text = proposal["proposed"].strip()
        if not text or (
            field != "advice" and (field in seen or text == current_value(item, field))
        ):
            continue
        seen.add(field)
        PageSuggestion.objects.create(
            workspace_id=item.workspace_id,
            item=item,
            analysis=analysis,
            field=field,
            current=current_value(item, field) if field != "advice" else "",
            proposed=text,
            reason=proposal["reason"],
            impact=proposal["impact"],
        )


def run_analysis(analysis: PageAnalysis) -> PageAnalysis:
    """Run a queued analysis to the end. Never raises for model or page problems."""
    item = analysis.item
    analysis.status = AnalysisStatus.RUNNING
    analysis.save(update_fields=["status", "updated_at"])
    neighbors = neighbors_of(item)
    try:
        review = ai.run_prompt(
            item.workspace_id,
            "content/page-analysis",
            ai.PageReview,
            memory_query=f"seo content {item.title}",
            user_message="Review the page.",
            site=ai.dumps(site_view(item.website)),
            page=ai.untrusted("website page", page_view(item)),
            checks=ai.dumps(checks.as_dicts(checks.run_checks(item))),
            neighbors=ai.dumps(neighbors),
            brief=ai.dumps(brief.merged(item.website, item)),
            search=ai.dumps(search.prompt_view(item.website, item.url)),
            instruction=analysis.instruction,
        )
    except DomainError as exc:
        log.warning("page_analysis_failed", item=str(item.pk), code=exc.code.value)
        analysis.status, analysis.error = AnalysisStatus.FAILED, exc.code.value
    else:
        data = _clean_review(review, {n["url"] for n in neighbors})
        analysis.result = data
        analysis.status = AnalysisStatus.DONE
        _store_suggestions(analysis, data)
    analysis.finished_at = timezone.now()
    analysis.save()
    return analysis


def rewrite_field(
    item: ContentItem, field: str, *, instruction: str = "", current: str | None = None
) -> ai.FieldOptions:
    """Three alternatives for one field, following the owner's instruction."""
    if field not in seo_meta.LIMITS:
        raise ValidationFailedError("this field cannot be rewritten")
    return ai.run_prompt(
        item.workspace_id,
        "content/field-rewrite",
        ai.FieldOptions,
        memory_query=f"{field} {item.title}",
        user_message="Write the options.",
        field=field,
        rules=FIELD_RULES.get(field, ""),
        page=ai.untrusted("website page", page_view(item, REWRITE_TEXT_CHARS)),
        current=current if current is not None else current_value(item, field),
        instruction=instruction[:500],
    )


def check_advice(item: ContentItem, check_key: str) -> ai.CheckAdvice:
    """Page-specific steps for fixing one failed or weak automatic check."""
    found = next((c for c in checks.run_checks(item) if c.key == check_key), None)
    if found is None:
        raise ValidationFailedError("unknown check")
    return ai.run_prompt(
        item.workspace_id,
        "content/check-advice",
        ai.CheckAdvice,
        memory_query=f"seo {check_key} {item.title}",
        user_message="Explain how to fix it.",
        check=ai.dumps(checks.as_dicts([found])[0]),
        page=ai.untrusted("website page", page_view(item, REWRITE_TEXT_CHARS)),
        brief=ai.dumps(brief.merged(item.website, item)),
    )


def latest_analysis(item: ContentItem) -> PageAnalysis | None:
    return PageAnalysis.objects.filter(item=item).first()


def dismiss(suggestion: PageSuggestion) -> None:
    suggestion.status = SuggestionStatus.DISMISSED
    suggestion.save(update_fields=["status", "updated_at"])


def mark_applied(item_id: uuid.UUID, fields: list[str]) -> None:
    """After a change reached the site, the matching open suggestions are done."""
    PageSuggestion.objects.filter(
        item_id=item_id, field__in=fields, status=SuggestionStatus.PROPOSED
    ).update(status=SuggestionStatus.APPLIED)
