"""Blog types. The guidance for each type lives in ``prompts/content/blog-types``."""

import re
from functools import lru_cache

from llm import prompts

BLOG_TYPES = (
    "ultimate_guide",
    "how_to",
    "listicle",
    "comparison",
    "case_study",
    "faq_answer",
    "trend_news",
    "opinion",
    "checklist_template",
    "local_service",
    "glossary",
)
DEFAULT_TYPE = "ultimate_guide"
HEADING = re.compile(r"^## (\w+)\s*$", re.M)


def is_valid(blog_type: str) -> bool:
    return blog_type in BLOG_TYPES


@lru_cache(maxsize=1)
def _guidance() -> dict[str, str]:
    body = prompts.get_prompt("content/blog-types").body
    parts = HEADING.split(body)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def guidance(blog_type: str) -> str:
    return _guidance().get(blog_type, "")


def catalog() -> str:
    """Every type with its first sentence, for the topic prompt."""
    return "\n".join(f"- {key}: {guidance(key).split('. ')[0]}." for key in BLOG_TYPES)


def clear_cache() -> None:
    _guidance.cache_clear()
