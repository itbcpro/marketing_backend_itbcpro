"""Shared plumbing for the AI tasks of the website domain.

Each task renders a versioned prompt (``prompts/content/...``), asks Claude for JSON that matches a
schema and records usage per workspace. Page text comes from outside the company, so it is fenced
as data; none of these tasks has tools, so nothing a page says can trigger an action.
"""

import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.tools import fence_untrusted
from llm import prompts
from llm.ports import ContentBlock, LlmRequest, Message, SystemBlock
from llm.service import get_llm_service
from llm.structured import complete_structured
from memory import services as memory
from workspaces.models import Workspace

AGENT = "seo-strategist"
CHANNEL = "website"
MAX_JSON_CHARS = 40_000
Level = Literal["high", "medium", "low"]


class Problem(BaseModel):
    severity: Level
    area: str
    evidence: str
    fix: str


class Proposal(BaseModel):
    field: Literal["seo_title", "meta_description", "title", "slug", "advice"]
    proposed: str
    reason: str
    impact: Level


class Faq(BaseModel):
    question: str
    answer: str


class LinkIdea(BaseModel):
    anchor: str
    target_url: str
    reason: str


class PageReview(BaseModel):
    summary: str
    verdict: Literal["strong", "good", "needs_work", "weak"]
    search_intent: Literal["informational", "commercial", "transactional", "navigational", "mixed"]
    primary_keyword: str
    secondary_keywords: list[str] = Field(max_length=6)
    strengths: list[str] = Field(max_length=6)
    problems: list[Problem] = Field(max_length=10)
    proposals: list[Proposal] = Field(max_length=10)
    faq: list[Faq] = Field(max_length=6)
    internal_links: list[LinkIdea] = Field(max_length=6)
    geo_tips: list[str] = Field(max_length=6)


class FieldOptions(BaseModel):
    options: list[str] = Field(min_length=1, max_length=5)
    note: str


class AltItem(BaseModel):
    index: int
    alt: str
    decorative: bool
    note: str


class AltSuggestions(BaseModel):
    items: list[AltItem] = Field(max_length=20)


class CheckAdvice(BaseModel):
    why_it_matters: str
    steps: list[str] = Field(min_length=1, max_length=8)
    example: str


def dumps(value: Any, limit: int = MAX_JSON_CHARS) -> str:
    text = json.dumps(value, indent=1, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def untrusted(source: str, value: Any) -> str:
    """Outside text, fenced so the model treats it as data."""
    return fence_untrusted(source, dumps(value))


def run_prompt[T: BaseModel](
    workspace_id: uuid.UUID,
    key: str,
    schema: type[T],
    *,
    memory_query: str,
    user_message: str,
    user_blocks: list[ContentBlock] | None = None,
    max_tokens: int | None = None,
    effort: str | None = None,
    **variables: object,
) -> T:
    """Render ``key`` with the brand profile and relevant memories and return validated output."""
    workspace = Workspace.objects.get(pk=workspace_id)
    prompt = prompts.get_prompt(key)
    system = prompt.render(
        workspace_name=workspace.name,
        brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace_id)),
        memories=memory.render_memories(
            memory.retrieve(
                workspace_id=workspace_id,
                query=memory_query,
                channel=CHANNEL,
                agent=AGENT,
                limit=8,
            )
        ),
        **variables,
    )
    request = LlmRequest(
        tier=prompt.tier,
        system=[SystemBlock(system)],
        messages=[Message("user", user_blocks) if user_blocks else Message.user(user_message)],
    )
    if max_tokens is not None:
        request.max_tokens = max_tokens
    if effort is not None:
        request.effort = effort
    result, _ = complete_structured(
        get_llm_service(workspace_id),
        request,
        schema,
        workspace_id=workspace_id,
        prompt_key=f"{key}@v{prompt.version}",
    )
    return result
