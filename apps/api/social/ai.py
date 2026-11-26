"""Shared plumbing for every AI task of the social domain.

Each task renders a versioned prompt (``prompts/social/...``), asks Claude for JSON that matches a
schema, and records usage per workspace. Untrusted text (comments, messages) is fenced by the
caller; none of these tasks has tools, so nothing a stranger writes can trigger an action.
"""

import json
import uuid
from typing import Any

from pydantic import BaseModel

from agents.tools import fence_untrusted
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import get_llm_service
from llm.structured import complete_structured
from memory import services as memory
from social.models import SocialAccount
from workspaces.models import Workspace

AGENT = "social-manager"
CHANNEL = "instagram"
MAX_JSON_CHARS = 60_000


def dumps(value: Any) -> str:
    text = json.dumps(value, indent=1, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_JSON_CHARS else text[:MAX_JSON_CHARS] + "\n[truncated]"


def untrusted(source: str, value: Any) -> str:
    """Outside text, fenced so the model treats it as data."""
    return fence_untrusted(source, dumps(value))


def account_view(account: SocialAccount) -> dict[str, Any]:
    return {
        "username": account.username,
        "name": account.name,
        "type": account.account_type,
        "followers": account.followers_count,
        "following": account.follows_count,
        "posts": account.media_count,
        "bio": account.biography,
        "website": account.website,
    }


def run_prompt[T: BaseModel](
    workspace_id: uuid.UUID,
    key: str,
    schema: type[T],
    *,
    memory_query: str,
    user_message: str,
    channel: str = CHANNEL,
    agent: str = AGENT,
    display_name: str | None = None,
    profile_text: str | None = None,
    **variables: object,
) -> T:
    """Render ``key`` with the brand profile and relevant memories and return validated output."""
    workspace = Workspace.objects.get(pk=workspace_id)
    prompt = prompts.get_prompt(key)
    system = prompt.render(
        workspace_name=display_name or workspace.name,
        brand_profile=profile_text
        if profile_text is not None
        else memory.render_brand_profile(memory.current_brand_profile(workspace_id)),
        memories=memory.render_memories(
            memory.retrieve(
                workspace_id=workspace_id,
                query=memory_query,
                channel=channel,
                agent=agent,
                limit=8,
            )
        ),
        **variables,
    )
    request = LlmRequest(
        tier=prompt.tier, system=[SystemBlock(system)], messages=[Message.user(user_message)]
    )
    result, _ = complete_structured(
        get_llm_service(workspace_id),
        request,
        schema,
        workspace_id=workspace_id,
        prompt_key=f"{key}@v{prompt.version}",
    )
    return result


def brand_profile_content(workspace_id: uuid.UUID) -> dict[str, Any]:
    profile = memory.current_brand_profile(workspace_id)
    return dict(profile.content) if profile else {}
