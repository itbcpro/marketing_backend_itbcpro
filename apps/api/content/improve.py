"""AI rewrite of one post's body: read it, rewrite it, check the result, keep it for review."""

import re

from django.utils import timezone
from pydantic import BaseModel, Field

from content import ai, body_html, brief, checks, review, search
from content.models import ContentItem, ItemSource, PostRevision, RevisionStatus, WebsiteKind
from content.sync import rest_base_for
from content.wordpress import factory
from core.errors import DomainError, ValidationFailedError
from core.logging import get_logger

log = get_logger(__name__)

MAX_HTML_CHARS = 60_000
MIN_WORD_SHARE = 0.85
MAX_NEW_LINKS = 5
BODY_CHECKS = {"headings", "internal_links", "readability", "h1"}


class Change(BaseModel):
    what: str
    why: str


class PostRewrite(BaseModel):
    html: str
    changes: list[Change] = Field(max_length=12)
    notes: str


def start(
    item: ContentItem, *, requested_by: str, instruction: str = "", focus: list[str] | None = None
) -> PostRevision:
    if item.source != ItemSource.WORDPRESS or item.website.kind != WebsiteKind.WORDPRESS:
        raise ValidationFailedError("only WordPress posts can be improved this way")
    wanted = [key for key in (focus or []) if key in BODY_CHECKS]
    return PostRevision.objects.create(
        workspace_id=item.workspace_id,
        item=item,
        instruction=instruction[:500],
        focus=wanted,
        requested_by=requested_by,
    )


def _failed(revision: PostRevision, code: str) -> PostRevision:
    revision.status, revision.error = RevisionStatus.FAILED, code
    revision.finished_at = timezone.now()
    revision.save()
    return revision


def _strip_foreign_links(html: str, allowed: set[str]) -> str:
    """Remove links to addresses that are neither in the original nor on the allowed list."""
    for address in body_html.links_of(html) - allowed:
        pattern = rf'<a\b[^>]*href="{re.escape(address)}"[^>]*>(.*?)</a>'
        html = re.sub(pattern, r"\1", html, flags=re.S)
    return html


def _ask(
    item: ContentItem, revision: PostRevision, raw: str, neighbors: list[dict[str, str]]
) -> PostRewrite:
    results = checks.run_checks(item)
    page = {"title": item.title, "address": item.url, "html": raw[:MAX_HTML_CHARS]}
    return ai.run_prompt(
        item.workspace_id,
        "content/post-improve",
        PostRewrite,
        memory_query=f"seo content {item.title}",
        user_message="Edit the post body.",
        max_tokens=32000,
        effort="medium",
        site=ai.dumps(review.site_view(item.website)),
        post=ai.untrusted("website post", page),
        checks=ai.dumps(checks.as_dicts(results)),
        neighbors=ai.dumps(neighbors),
        brief=ai.dumps(brief.merged(item.website, item)),
        search=ai.dumps(search.prompt_view(item.website, item.url)),
        focus=ai.dumps(revision.focus),
        instruction=revision.instruction or "(none)",
    )


def run(revision: PostRevision) -> PostRevision:
    """Run a queued rewrite to the end. Never raises for site or model problems."""
    item = revision.item
    revision.status = RevisionStatus.RUNNING
    revision.save(update_fields=["status", "updated_at"])
    try:
        client = factory.for_connection(item.website.connection)  # type: ignore[arg-type]
        row = client.item_body(rest_base_for(client, item), int(item.external_id))
        raw = str((row.get("content") or {}).get("raw", ""))
        if not raw.strip():
            return _failed(revision, "empty_post")
        if body_html.is_builder_post(row):
            return _failed(revision, "page_builder_post")
        if body_html.risky_reason(raw):
            return _failed(revision, body_html.risky_reason(raw))
        neighbors = review.neighbors_of(item)
        result = _ask(item, revision, raw, neighbors)
    except DomainError as exc:
        log.warning("post_improve_failed", item=str(item.pk), code=exc.code.value)
        return _failed(revision, str(exc)[:200] or exc.code.value)
    return _store(revision, raw, str(row.get("modified_gmt", "")), result, neighbors)


def _store(
    revision: PostRevision,
    raw: str,
    modified: str,
    result: PostRewrite,
    neighbors: list[dict[str, str]],
) -> PostRevision:
    allowed = {n["url"] for n in neighbors} | body_html.links_of(raw)
    clean = _strip_foreign_links(body_html.clean_html(result.html), allowed)
    clean = body_html.demote_new_h1(raw, clean)
    before_words, after_words = body_html.word_count(raw), body_html.word_count(clean)
    if not clean or after_words < MIN_WORD_SHARE * before_words:
        return _failed(revision, "rewrite_too_short")
    if body_html.lost_assets(raw, clean):
        return _failed(revision, "rewrite_lost_links")
    added = sorted(body_html.new_links(raw, clean))[:MAX_NEW_LINKS]
    revision.before_html, revision.after_html = raw, clean
    revision.base_modified_at = modified
    revision.changes = [c.model_dump() for c in result.changes]
    revision.notes = result.notes
    revision.stats = {
        "before_words": before_words,
        "after_words": after_words,
        "added_links": added,
        "headings_before": len(re.findall(r"<h[2-4]\b", raw, re.I)),
        "headings_after": len(re.findall(r"<h[2-4]\b", clean, re.I)),
    }
    revision.status, revision.finished_at = RevisionStatus.DONE, timezone.now()
    revision.save()
    return revision
