"""AI draft of a whole Search campaign from the owner's plain description.

The model writes the draft by the best-practice prompt; everything it returns is then cleaned
against Google's limits (text length, duplicates, known countries and languages, keyword rules),
so the form is filled with valid values. Nothing is created in Google Ads: the owner reviews and
edits the draft, then sends it for approval like any other campaign.
"""

import json
from typing import Any, Literal
from urllib.parse import urlsplit

from ninja import Schema
from pydantic import Field

from ads.google import landing
from ads.google.schemas import (
    COUNTRY_IDS,
    DESCRIPTION_CHARS,
    HEADLINE_CHARS,
    LANGUAGE_IDS,
    MIN_BUDGET_MICROS,
    PATH_CHARS,
    Keyword,
)
from ads.models import AdEntity, AdLevel
from ads.openai.review import AGENT
from connections.models import Connection
from core.errors import ValidationFailedError
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import get_llm_service
from llm.structured import complete_structured
from memory import services as memory

PROMPT_KEY = "ads/google-campaign"
GROUP_PROMPT_KEY = "ads/google-ad-group"
MAX_BRIEF_CHARS = 12000
MICROS = 1_000_000
MAX_HEADLINES = 15
MAX_DESCRIPTIONS = 4
MIN_HEADLINES = 3
MIN_DESCRIPTIONS = 2
MIN_NAME = 3
MAX_ADS = 3
MAX_GROUPS = 6
MAX_CAMPAIGNS = 3
MAX_CONTEXT_KEYWORDS = 25


class DraftKeyword(Schema):
    text: str
    match_type: Literal["exact", "phrase", "broad"]


class DraftAd(Schema):
    final_url: str = ""
    headlines: list[str]
    descriptions: list[str]
    path1: str = ""
    path2: str = ""


class DraftGroup(Schema):
    name: str
    keywords: list[DraftKeyword]
    negative_keywords: list[str] = Field(default_factory=list)
    ads: list[DraftAd]


class GroupDraft(Schema):
    summary: str
    notes: str = ""
    ad_group: DraftGroup


class DraftCampaign(Schema):
    name: str
    daily_budget: float = Field(gt=0)
    bidding: Literal["manual_cpc", "maximize_clicks", "maximize_conversions"]
    max_cpc: float | None = None
    countries: list[str]
    languages: list[str]
    negative_keywords: list[str]
    ad_groups: list[DraftGroup]


class Draft(Schema):
    summary: str
    notes: str = ""
    campaigns: list[DraftCampaign]


def _page(url: str) -> dict[str, Any]:
    """Facts from the owner's landing page, when one is given and can be read."""
    if not url:
        return {}
    audited = landing.audit(url, [])
    facts = audited.get("facts") or {}
    return {
        "url": url,
        "reachable": audited.get("reachable", False),
        "title": facts.get("title", ""),
        "h1": facts.get("h1", ""),
        "excerpt": str(facts.get("excerpt", ""))[:900],
    }


def _texts(values: list[str], limit: int, cap: int) -> list[str]:
    """Trimmed, distinct, within the character limit and capped in number."""
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        text = " ".join(value.split())
        if text and len(text) <= limit and text.lower() not in seen:
            seen.add(text.lower())
            kept.append(text)
    return kept[:cap]


def _keywords(items: list[DraftKeyword]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    kept: list[dict[str, str]] = []
    for item in items:
        try:
            clean = Keyword(text=item.text, match_type=item.match_type)
        except ValueError:
            continue
        key = (clean.text.lower(), clean.match_type)
        if key not in seen:
            seen.add(key)
            kept.append({"text": clean.text, "match_type": clean.match_type})
    return kept


def _negatives(values: list[str], blocked: set[str]) -> list[dict[str, str]]:
    """Phrase-match negatives, skipping any that would block one of the campaign's own keywords."""
    found = _keywords([DraftKeyword(text=n, match_type="phrase") for n in values])
    return [k for k in found if k["text"].lower() not in blocked]


def _ads(group: DraftGroup, url: str) -> list[dict[str, Any]]:
    """Up to three valid ads: enough headlines and descriptions, an https page, short paths."""
    kept: list[dict[str, Any]] = []
    for ad in group.ads:
        headlines = _texts(ad.headlines, HEADLINE_CHARS, MAX_HEADLINES)
        descriptions = _texts(ad.descriptions, DESCRIPTION_CHARS, MAX_DESCRIPTIONS)
        if len(headlines) < MIN_HEADLINES or len(descriptions) < MIN_DESCRIPTIONS:
            continue
        own = ad.final_url.strip()
        kept.append(
            {
                "final_url": url or (own if own.startswith("https://") else ""),
                "headlines": headlines,
                "descriptions": descriptions,
                "path1": ad.path1.strip() if len(ad.path1.strip()) <= PATH_CHARS else "",
                "path2": ad.path2.strip() if len(ad.path2.strip()) <= PATH_CHARS else "",
            }
        )
    return kept[:MAX_ADS]


def _groups(result: DraftCampaign, url: str) -> list[dict[str, Any]]:
    """Valid ad groups: unique names, keywords not repeated across groups, at least one ad."""
    seen_keywords: set[tuple[str, str]] = set()
    names: set[str] = set()
    kept: list[dict[str, Any]] = []
    for group in result.ad_groups:
        name = group.name.strip()[:200]
        ads = _ads(group, url)
        keywords = [
            k
            for k in _keywords(group.keywords)
            if (k["text"].lower(), k["match_type"]) not in seen_keywords
        ]
        if len(name) < MIN_NAME or name.lower() in names or not keywords or not ads:
            continue
        names.add(name.lower())
        seen_keywords |= {(k["text"].lower(), k["match_type"]) for k in keywords}
        kept.append({"name": name, "keywords": keywords, "negative_keywords": [], "ads": ads})
    return kept[:MAX_GROUPS]


def _campaign(result: DraftCampaign, url: str) -> dict[str, Any]:
    """One campaign as form values: valid, deduplicated, money in micros."""
    groups = _groups(result, url)
    theme = {k["text"].lower() for g in groups for k in g["keywords"]}
    for source, group in zip(result.ad_groups, groups, strict=False):
        if source.name.strip()[:200] == group["name"]:
            group["negative_keywords"] = _negatives(source.negative_keywords, theme)
    countries = [c.strip().upper() for c in result.countries if c.strip().upper() in COUNTRY_IDS]
    languages = [c.strip().lower() for c in result.languages if c.strip().lower() in LANGUAGE_IDS]
    return {
        "name": result.name.strip()[:200],
        "daily_budget_micros": max(MIN_BUDGET_MICROS, round(result.daily_budget * MICROS)),
        "bidding": result.bidding,
        "max_cpc_micros": (
            round(result.max_cpc * MICROS)
            if result.bidding == "manual_cpc" and result.max_cpc and result.max_cpc > 0
            else None
        ),
        "countries": countries or ["CA"],
        "languages": languages or ["en"],
        "negative_keywords": _negatives(result.negative_keywords, theme),
        "ad_groups": groups,
    }


def cleaned(result: Draft, url: str) -> dict[str, Any]:
    """The whole draft as form values: only campaigns that still have an ad group are kept."""
    campaigns = [_campaign(c, url) for c in result.campaigns[:MAX_CAMPAIGNS]]
    kept = [c for c in campaigns if c["ad_groups"] and len(c["name"]) >= MIN_NAME]
    return {"summary": result.summary, "notes": result.notes, "campaigns": kept}


def draft(connection: Connection, brief: str, url: str, currency: str) -> dict[str, Any]:
    text = brief.strip()
    if not text:
        raise ValidationFailedError("describe what you want to advertise")
    if len(text) > MAX_BRIEF_CHARS:
        raise ValidationFailedError("the description is too long")
    link = url.strip()
    if link and (urlsplit(link).scheme != "https" or not urlsplit(link).hostname):
        raise ValidationFailedError("the website address must start with https://")
    workspace = connection.workspace
    prompt = prompts.get_prompt(PROMPT_KEY)
    system = prompt.render(
        workspace_name=workspace.name,
        brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
        memories=memory.render_memories(
            memory.retrieve(
                workspace_id=workspace.pk,
                query=f"google ads search campaign {text[:200]}",
                agent=AGENT,
                limit=6,
            )
        ),
        brief=text,
        currency=currency or "the account currency",
        page=json.dumps(_page(link), indent=1, default=str),
    )
    request = LlmRequest(
        tier=prompt.tier,
        system=[SystemBlock(system)],
        messages=[Message.user("Answer with the campaign draft.")],
    )
    result, _ = complete_structured(
        get_llm_service(workspace.pk),
        request,
        Draft,
        workspace_id=workspace.pk,
        prompt_key=f"{PROMPT_KEY}@v{prompt.version}",
    )
    out = cleaned(result, link)
    if not out["campaigns"]:
        raise ValidationFailedError("the AI could not write a valid campaign; try again")
    return out


def _campaign_context(campaign: AdEntity) -> dict[str, Any]:
    """The campaign's name and what each existing ad group already targets."""
    groups = []
    for group in campaign.children.filter(level=AdLevel.AD_GROUP, missing_since__isnull=True):
        words = group.children.filter(level=AdLevel.KEYWORD, missing_since__isnull=True)
        names = list(words.values_list("name", flat=True)[:MAX_CONTEXT_KEYWORDS])
        groups.append({"name": group.name, "keywords": names})
    return {"campaign": campaign.name, "ad_groups": groups}


def draft_group(
    connection: Connection, campaign: AdEntity, brief: str, url: str, currency: str
) -> dict[str, Any]:
    """One more ad group for an existing campaign, written from the owner's plain description."""
    text = brief.strip()
    if not text:
        raise ValidationFailedError("describe what the new ad group should target")
    if len(text) > MAX_BRIEF_CHARS:
        raise ValidationFailedError("the description is too long")
    link = url.strip()
    if link and (urlsplit(link).scheme != "https" or not urlsplit(link).hostname):
        raise ValidationFailedError("the website address must start with https://")
    workspace = connection.workspace
    prompt = prompts.get_prompt(GROUP_PROMPT_KEY)
    system = prompt.render(
        workspace_name=workspace.name,
        brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
        memories=memory.render_memories(
            memory.retrieve(
                workspace_id=workspace.pk,
                query=f"google ads search ad group {text[:200]}",
                agent=AGENT,
                limit=6,
            )
        ),
        brief=text,
        currency=currency or "the account currency",
        page=json.dumps(_page(link), indent=1, default=str),
        campaign=json.dumps(_campaign_context(campaign), indent=1, default=str),
    )
    request = LlmRequest(
        tier=prompt.tier,
        system=[SystemBlock(system)],
        messages=[Message.user("Answer with the ad group.")],
    )
    result, _ = complete_structured(
        get_llm_service(workspace.pk),
        request,
        GroupDraft,
        workspace_id=workspace.pk,
        prompt_key=f"{GROUP_PROMPT_KEY}@v{prompt.version}",
    )
    taken = {
        name.lower()
        for name in AdEntity.objects.filter(
            connection=connection, level=AdLevel.KEYWORD, missing_since__isnull=True
        ).values_list("name", flat=True)
    }
    shell = DraftCampaign(
        name="",
        daily_budget=1,
        bidding="maximize_clicks",
        countries=[],
        languages=[],
        negative_keywords=[],
        ad_groups=[result.ad_group],
    )
    groups = _groups(shell, link)
    if not groups:
        raise ValidationFailedError("the AI could not write a valid ad group; try again")
    group = groups[0]
    group["keywords"] = [k for k in group["keywords"] if k["text"].lower() not in taken]
    if not group["keywords"]:
        raise ValidationFailedError("every keyword it wrote already exists in this account")
    group["negative_keywords"] = _negatives(
        result.ad_group.negative_keywords, {k["text"].lower() for k in group["keywords"]}
    )
    return {"summary": result.summary, "notes": result.notes, "ad_group": group}
