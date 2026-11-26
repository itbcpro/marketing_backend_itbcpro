"""Topic suggestions: read what the blog already covers and propose what to write next."""

import uuid
from typing import Any

from content import ai
from content.blog import context, kinds
from content.blog.schemas import TopicIdeas
from content.models import ArticleIdea, IdeaStatus, Website
from core.errors import ValidationFailedError

MAX_COUNT = 15
DEFAULT_COUNT = 8
AVOID_LIMIT = 120


def _avoid(website: Website) -> list[str]:
    rows = ArticleIdea.objects.filter(website=website).order_by("-created_at")[:AVOID_LIMIT]
    return [r.title for r in rows if r.status != IdeaStatus.SUGGESTED] + [
        r.title for r in rows if r.status == IdeaStatus.SUGGESTED
    ]


def _clean_type(value: str, forced: str) -> str:
    if forced:
        return forced
    return value if kinds.is_valid(value) else kinds.DEFAULT_TYPE


def suggest(
    website: Website,
    *,
    requested_by: str,
    focus: str = "",
    count: int = DEFAULT_COUNT,
    blog_type: str = "",
) -> list[ArticleIdea]:
    """Ask the strategist for ``count`` ideas and store them as one batch."""
    if blog_type and not kinds.is_valid(blog_type):
        raise ValidationFailedError("unknown blog type")
    count = max(1, min(count, MAX_COUNT))
    posts = context.existing_posts(website)
    known = {p["address"] for p in posts}
    result = ai.run_prompt(
        website.workspace_id,
        "content/topic-ideas",
        TopicIdeas,
        memory_query=f"blog topics {website.name} {focus}",
        user_message="Suggest the topics.",
        site=ai.dumps(context.site_view(website)),
        posts=ai.untrusted("website posts", posts),
        search=ai.dumps(context.search_evidence(website)),
        brief=ai.dumps(website.brief or {}),
        blog_types=kinds.catalog(),
        focus=focus[:500] or "(no preference)",
        count=count,
        avoid=ai.dumps(_avoid(website)),
    )
    return _store(website, result, known, requested_by, blog_type)[:count]


def _store(
    website: Website, result: TopicIdeas, known: set[str], requested_by: str, forced_type: str
) -> list[ArticleIdea]:
    batch = uuid.uuid4()
    taken = {t.lower() for t in _avoid(website)}
    rows: list[ArticleIdea] = []
    for idea in result.ideas:
        title = idea.title.strip()
        if not title or title.lower() in taken:
            continue
        taken.add(title.lower())
        data: dict[str, Any] = idea.model_dump()
        data["title"] = title
        data["blog_type"] = _clean_type(idea.blog_type, forced_type)
        data["overlaps"] = [u for u in idea.overlaps if u in known]
        rows.append(
            ArticleIdea(
                workspace_id=website.workspace_id,
                website=website,
                batch=batch,
                requested_by=requested_by,
                **data,
            )
        )
    return ArticleIdea.objects.bulk_create(rows)
