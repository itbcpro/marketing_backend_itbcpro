"""AI suggestions for a new ad group or a new ad inside an existing campaign.

Nothing is written here: the suggestion fills the add form, and the owner reviews it and creates the
item through the normal paused-first, approval-gated change flow.
"""

import json
from typing import Any, Literal

from ninja import Schema
from pydantic import Field, field_validator

from ads.models import AdEntity, AdLevel
from ads.openai import rows as row_builder
from ads.openai.checks import FRAGMENT_STARTS
from ads.openai.review import AGENT
from core.errors import NotFoundError, ValidationFailedError
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import get_llm_service
from llm.structured import complete_structured
from memory import services as memory
from workspaces.models import Workspace

GROUP_PROMPT = "ads/openai-new-group"
AD_PROMPT = "ads/openai-new-ad"
MAX_INSTRUCTION_CHARS = 1000
MIN_HINTS, MAX_HINTS = 8, 20
MIN_ADS, MAX_ADS = 2, 6
HINT_MIN_CHARS, HINT_MAX_CHARS = 3, 200
TITLE_MAX, BODY_MAX = 50, 100
OVERLAP_WARN = 0.4
CAMPAIGN_KEYS = ("name", "status", "description", "budget", "bidding_type", "targeting")
PERF_KEYS = ("impressions_7d", "clicks_7d", "ctr_7d")


class GroupAd(Schema):
    """One ad written together with its new ad group, so the group can be created with its ads."""

    name: str = Field(min_length=3, max_length=200)
    title: str = Field(min_length=3, max_length=TITLE_MAX)
    body: str = Field(min_length=1, max_length=BODY_MAX)
    url: str = Field(default="", max_length=2048, pattern=r"^(https?://.*)?$")
    angle: str


class GroupSuggestion(Schema):
    name: str = Field(min_length=3, max_length=200)
    theme: str
    context_hints: list[str] = Field(min_length=MIN_HINTS, max_length=MAX_HINTS)
    strategy: Literal["maximize_clicks", "fixed_bid"]
    max_bid: float | None
    ads: list[GroupAd] = Field(min_length=MIN_ADS, max_length=MAX_ADS)
    rationale: str
    notes: str

    @field_validator("ads")
    @classmethod
    def distinct(cls, value: list[GroupAd]) -> list[GroupAd]:
        titles = [a.title.strip().lower() for a in value]
        if len(set(titles)) != len(titles):
            raise ValueError("every ad needs a different headline")
        return value

    @field_validator("context_hints")
    @classmethod
    def clean(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for hint in value:
            text = hint.strip()
            if not HINT_MIN_CHARS <= len(text) <= HINT_MAX_CHARS:
                raise ValueError(f"each hint must be {HINT_MIN_CHARS} to {HINT_MAX_CHARS} chars")
            if text.lower().startswith(FRAGMENT_STARTS):
                raise ValueError(f"a hint must not start with a conjunction: {text[:40]!r}")
            if text.lower() in seen:
                raise ValueError(f"duplicate hint: {text[:40]!r}")
            seen.add(text.lower())
        return [h.strip() for h in value]


class AdSuggestion(Schema):
    name: str = Field(min_length=3, max_length=200)
    title: str = Field(min_length=3, max_length=TITLE_MAX)
    body: str = Field(min_length=1, max_length=BODY_MAX)
    url: str = Field(default="", max_length=2048, pattern=r"^(https?://.*)?$")
    angle: str
    rationale: str
    notes: str


class NewGroupSuggestion(GroupSuggestion):
    warnings: list[str]


class NewAdSuggestion(AdSuggestion):
    warnings: list[str]


def _perf(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in PERF_KEYS}


def _entity(connection_id: Any, external_id: str, level: str) -> AdEntity:
    found = (
        AdEntity.objects.filter(connection_id=connection_id, external_id=external_id, level=level)
        .select_related("connection", "workspace", "parent")
        .first()
    )
    if found is None:
        raise NotFoundError("not found in the synced account; sync first")
    return found


def context(campaign: AdEntity, focus: AdEntity | None = None) -> dict[str, Any]:
    """The campaign, every ad group under it and every ad under those, with 7-day results."""
    groups = list(campaign.children.filter(missing_since__isnull=True))
    ads = list(AdEntity.objects.filter(parent__in=groups, missing_since__isnull=True))
    rows = row_builder.build_rows([campaign, *groups, *ads])
    by_group: dict[Any, list[AdEntity]] = {}
    for ad in ads:
        by_group.setdefault(ad.parent_id, []).append(ad)
    return {
        "campaign": {
            **{k: campaign.data.get(k) for k in CAMPAIGN_KEYS},
            "performance": _perf(rows[campaign.pk]),
        },
        "ad_groups": [
            {
                "name": g.name,
                "status": g.status,
                "is_the_group_the_new_ad_goes_in": focus is not None and g.pk == focus.pk,
                "context_hints": g.data.get("context_hints") or [],
                "bidding": g.data.get("bidding_config"),
                "performance": _perf(rows[g.pk]),
                "ads": [
                    {
                        "name": a.name,
                        "status": a.status,
                        "review_status": a.review_status,
                        "title": (a.data.get("creative") or {}).get("title"),
                        "body": (a.data.get("creative") or {}).get("body"),
                        "url": (a.data.get("creative") or {}).get("target_url"),
                        "performance": _perf(rows[a.pk]),
                    }
                    for a in by_group.get(g.pk, [])
                ],
            }
            for g in groups
        ],
    }


def _ask[M: Schema](
    workspace: Workspace, key: str, model: type[M], snapshot: dict[str, Any], instruction: str
) -> M:
    text = instruction.strip()
    if len(text) > MAX_INSTRUCTION_CHARS:
        raise ValidationFailedError("the request is too long")
    prompt = prompts.get_prompt(key)
    system = prompt.render(
        workspace_name=workspace.name,
        brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
        memories=memory.render_memories(
            memory.retrieve(
                workspace_id=workspace.pk, query="new ad group or ad", agent=AGENT, limit=6
            )
        ),
        snapshot=json.dumps(snapshot, indent=1, default=str),
        instruction=text,
    )
    request = LlmRequest(
        tier=prompt.tier,
        system=[SystemBlock(system)],
        messages=[Message.user("Answer with the structured suggestion.")],
    )
    result, _ = complete_structured(
        get_llm_service(workspace.pk),
        request,
        model,
        workspace_id=workspace.pk,
        prompt_key=f"{key}@v{prompt.version}",
    )
    return result


def suggest_group(
    campaign_external_id: str, connection_id: Any, instruction: str
) -> NewGroupSuggestion:
    campaign = _entity(connection_id, campaign_external_id, AdLevel.CAMPAIGN)
    snapshot = context(campaign)
    result = _ask(campaign.workspace, GROUP_PROMPT, GroupSuggestion, snapshot, instruction)
    warnings: list[str] = []
    if any(not ad.url for ad in result.ads):
        warnings.append("No landing page link is known yet. Type the real link in each ad.")
    existing = snapshot["ad_groups"]
    if result.name.strip().lower() in {g["name"].strip().lower() for g in existing}:
        warnings.append("An ad group with this name already exists in the campaign.")
    used_titles = {(a["title"] or "").strip().lower() for g in existing for a in g["ads"]}
    repeated = [a.title for a in result.ads if a.title.strip().lower() in used_titles]
    if repeated:
        warnings.append(f"These headlines already exist in the campaign: {', '.join(repeated)}.")
    taken = {h.strip().lower() for g in existing for h in g["context_hints"]}
    shared = [h for h in result.context_hints if h.strip().lower() in taken]
    if len(shared) / len(result.context_hints) >= OVERLAP_WARN:
        warnings.append(
            f"{len(shared)} of {len(result.context_hints)} hints already appear in other ad "
            "groups of this campaign, so the groups may compete with each other."
        )
    return NewGroupSuggestion(**result.model_dump(), warnings=warnings)


def suggest_ad(group_external_id: str, connection_id: Any, instruction: str) -> NewAdSuggestion:
    group = _entity(connection_id, group_external_id, AdLevel.AD_GROUP)
    if group.parent is None:
        raise NotFoundError("the ad group has no campaign in the synced account; sync first")
    snapshot = context(group.parent, focus=group)
    result = _ask(group.workspace, AD_PROMPT, AdSuggestion, snapshot, instruction)
    warnings: list[str] = []
    if not result.url:
        warnings.append(
            "No landing page link is known yet. Type the real link before you create it."
        )
    used = {(a["title"] or "").strip().lower() for g in snapshot["ad_groups"] for a in g["ads"]}
    if result.title.strip().lower() in used:
        warnings.append("An ad in this campaign already uses this headline.")
    return NewAdSuggestion(**result.model_dump(), warnings=warnings)
