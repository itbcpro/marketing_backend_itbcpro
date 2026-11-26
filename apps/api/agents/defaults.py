"""Default agent roster for a workspace. Definitions are data and can be edited later."""

import uuid
from typing import Any

from agents.models import AgentDefinition

OPENAI_ADS_TOOLS = [
    "openai_ads_overview",
    "openai_ads_insights",
    "openai_ads_create_campaign",
    "openai_ads_create_ad_group",
    "openai_ads_create_ad",
    "openai_ads_upload_image",
    "openai_ads_activate",
    "openai_ads_pause",
    "openai_ads_archive",
    "openai_ads_update_budget",
    "openai_ads_update_bid",
    "openai_ads_update_campaign",
    "openai_ads_update_ad_group",
    "openai_ads_update_ad",
]

INSTAGRAM_TOOLS = [
    "instagram_overview",
    "instagram_stats",
    "instagram_posts",
    "instagram_inbox",
    "instagram_draft_post",
    "instagram_reply_comment",
    "instagram_hide_comment",
    "instagram_delete_comment",
    "instagram_set_comments_enabled",
    "instagram_send_message",
]

DEFAULT_AGENTS: list[dict[str, Any]] = [
    {
        "name": "orchestrator",
        "title": "Orchestrator",
        "prompt_key": "agents/orchestrator",
        "tier": "planner",
        "tools": ["memory_search", "delegate"],
        "max_steps": 16,
    },
    {
        "name": "ads-strategist",
        "title": "Advertising strategist",
        "prompt_key": "agents/ads-strategist",
        "tier": "planner",
        "tools": ["memory_search", *OPENAI_ADS_TOOLS],
        "channel": "ads",
    },
    {
        "name": "social-manager",
        "title": "Instagram manager",
        "prompt_key": "agents/social-manager",
        "tier": "planner",
        "tools": ["memory_search", *INSTAGRAM_TOOLS],
        "channel": "instagram",
    },
    {
        "name": "community-responder",
        "title": "Community responder",
        "prompt_key": "agents/community-responder",
        "tier": "writer",
        "tools": [
            "memory_search",
            "instagram_overview",
            "instagram_inbox",
            "instagram_reply_comment",
            "instagram_hide_comment",
            "instagram_send_message",
        ],
        "channel": "instagram",
    },
]


def ensure_default_agents(workspace_id: uuid.UUID) -> list[AgentDefinition]:
    created: list[AgentDefinition] = []
    for spec in DEFAULT_AGENTS:
        definition, _ = AgentDefinition.objects.get_or_create(
            workspace_id=workspace_id, name=spec["name"], defaults=spec
        )
        created.append(definition)
    return created
