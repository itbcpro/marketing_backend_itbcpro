"""Ad assistant: Claude turns a plain-language goal into a paused-first OpenAI Ads draft.

Nothing here writes to the platform. ``suggest`` produces a draft the owner can refine; ``launch``
hands the approved draft to the ads strategist, whose proposals then wait in Approvals.
"""

import json
import re
import uuid
from typing import Any, Literal
from urllib.parse import urlparse

from ninja import Schema
from pydantic import Field

from actions.models import Guardrail
from ads.openai import actions
from agents import runtime
from agents.models import AgentRun
from connections.models import Connection
from core.errors import DomainError
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import LlmService
from llm.structured import complete_structured
from memory import services as memory
from workspaces.models import Workspace

PROMPT_KEY = "ads/openai-assist"
AGENT = "ads-strategist"
MICROS = 1_000_000
MAX_GROUPS = 10
MAX_ADS = 10
MAX_TOTAL_ADS = 40
MAX_PLACES = 60
MAX_LOOKUP = 100
MAX_AREAS_PER_PLACE = 100
CITY_PARTS = 3
MAX_CAMPAIGNS_LISTED = 15
MIN_DAILY_BUDGET = 25.0
ATTEMPTS = 2
COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
CA_POSTAL = re.compile(r"^[A-Za-z]\d[A-Za-z]([ -]?\d[A-Za-z]\d)?$")
US_ZIP = re.compile(r"^\d{5}(-\d{4})?$")


class DraftPlace(Schema):
    """A place to target (or avoid). The model names it; the geo lookup supplies the id."""

    query: str = Field(min_length=2, max_length=120)
    reason: str = ""
    ids: list[str] = Field(default_factory=list)
    name: str | None = None
    kind: str | None = None


class DraftAd(Schema):
    name: str = Field(min_length=3, max_length=200)
    title: str = Field(min_length=3, max_length=50)
    body: str = Field(min_length=1, max_length=100)
    url: str = Field(max_length=2048, pattern=r"^https://")


class DraftAdGroup(Schema):
    name: str = Field(min_length=3, max_length=200)
    theme: str
    context_hints: list[str] = Field(min_length=1, max_length=60)
    strategy: Literal["maximize_clicks", "fixed_bid"]
    max_bid: float | None
    rationale: str
    ads: list[DraftAd] = Field(min_length=1, max_length=MAX_ADS)


class Draft(Schema):
    campaign_name: str = Field(min_length=3, max_length=200)
    objective: Literal["clicks", "impressions"]
    daily_budget: float = Field(ge=1)
    countries: list[str]
    platforms: list[Literal["web", "ios_app", "android_app"]]
    rationale: str
    ad_groups: list[DraftAdGroup] = Field(min_length=1, max_length=MAX_GROUPS)
    checks: list[str]
    warnings: list[str]
    description: str = Field(default="", max_length=500)
    places: list[DraftPlace] = Field(default_factory=list, max_length=MAX_PLACES)
    excluded_places: list[DraftPlace] = Field(default_factory=list, max_length=MAX_PLACES)
    requested_ads: int | None = Field(default=None, ge=1, le=1000)
    requested_ad_groups: int | None = Field(default=None, ge=1, le=1000)

    @property
    def location_ids(self) -> list[str]:
        return list(dict.fromkeys(i for place in self.places for i in place.ids))

    @property
    def excluded_location_ids(self) -> list[str]:
        return list(dict.fromkeys(i for place in self.excluded_places for i in place.ids))

    @property
    def ad_count(self) -> int:
        return sum(len(group.ads) for group in self.ad_groups)


class Suggestion(Schema):
    status: Literal["needs_input", "draft"]
    questions: list[str]
    draft: Draft | None


def _account_summary(connection: Connection) -> tuple[str, dict[str, Any]]:
    with actions.client_factory(connection) as client:
        account = client.get_account()
        campaigns = client.list_resource("campaigns")
    lines = [
        f"Account: {account.get('account_name') or account.get('name')}",
        f"Currency: {account.get('currency_code')}",
        f"Website on file: {account.get('url') or 'unknown'}",
        f"Existing campaigns: {len(campaigns)}",
    ]
    for campaign in campaigns[:MAX_CAMPAIGNS_LISTED]:
        budget = campaign.get("budget") or {}
        daily = budget.get("daily_spend_limit_micros")
        spend = f"{daily / MICROS:.2f} per day" if daily else "lifetime budget"
        lines.append(f"- {campaign.get('name')} ({campaign.get('status')}, {spend})")
    return "\n".join(lines), account


def _postal_key(query: str) -> str | None:
    """The catalogue key for a postal code: Canada by its first three characters, US by ZIP."""
    text = query.strip()
    if CA_POSTAL.match(text):
        return text.replace(" ", "").replace("-", "")[:3].upper()
    if US_ZIP.match(text):
        return text[:5]
    return None


def _in_countries(row: dict[str, Any], countries: list[str]) -> bool:
    return not countries or row.get("country_code") in countries


def _match_postal(
    key: str, rows: list[dict[str, Any]], countries: list[str]
) -> list[dict[str, Any]]:
    same = [
        r
        for r in rows
        if r.get("type") == "postal_code" and str(r.get("name", "")).casefold() == key.casefold()
    ]
    return [r for r in same if _in_countries(r, countries)][:1] or same[:1]


def _match_area(
    query: str, rows: list[dict[str, Any]], countries: list[str]
) -> list[dict[str, Any]]:
    """A country or region by name, else every postal area of a city ("Vancouver, BC")."""
    wanted = query.strip().casefold()
    for row in rows:
        names = {
            str(row.get("name", "")).casefold(),
            str(row.get("canonical_name", "")).casefold(),
        }
        is_area = row.get("type") in {"country", "region", "market"}
        if is_area and wanted in names and _in_countries(row, countries):
            return [row]
    city, _, province = (part.strip().casefold() for part in wanted.partition(","))
    areas = []
    for row in rows:
        if row.get("type") != "postal_code" or not _in_countries(row, countries):
            continue
        parts = [p.strip().casefold() for p in str(row.get("canonical_name", "")).split(",")]
        if (
            len(parts) >= CITY_PARTS
            and parts[1] == city
            and (not province or province in parts[2:])
        ):
            areas.append(row)
    return areas[:MAX_AREAS_PER_PLACE]


def _resolve_places(
    connection: Connection, places: list[DraftPlace], countries: list[str], label: str
) -> list[str]:
    """Fill in ids from the platform's geo lookup; return warnings for what could not be placed."""
    warnings: list[str] = []
    if not places:
        return warnings
    with actions.client_factory(connection) as client:
        for place in places:
            if place.ids:
                continue
            key = _postal_key(place.query)
            try:
                rows = client.geo_lookup(key or place.query, limit=MAX_LOOKUP)
            except DomainError:
                warnings.append(f"Could not look up the {label} place '{place.query}' right now.")
                continue
            found = (
                _match_postal(key, rows, countries)
                if key
                else _match_area(place.query, rows, countries)
            )
            if not found:
                hint = " Try the city with its province, like 'Burnaby, BC'." if not key else ""
                warnings.append(f"No targetable {label} place matches '{place.query}'.{hint}")
                continue
            place.ids = [str(row["id"]) for row in found]
            place.kind = str(found[0].get("type") or "") or None
            place.name = (
                str(found[0].get("canonical_name") or place.query)
                if len(found) == 1
                else f"{place.query} ({len(found)} postal areas)"
            )
    return warnings


def _reach_warnings(draft: Draft) -> list[str]:
    if draft.location_ids:
        return []
    return [
        "No place could be matched, so this campaign would run across all of "
        f"{', '.join(draft.countries) or 'the account country'}. Fix the places before launching."
    ]


def _count_problem(draft: Draft) -> str:
    """What is wrong with the number of ads or groups, in words the model can act on."""
    problems: list[str] = []
    ads = min(draft.requested_ads, MAX_TOTAL_ADS) if draft.requested_ads else None
    if ads and draft.ad_count != ads:
        problems.append(f"it has {draft.ad_count} ads in total but exactly {ads} are required")
    groups = min(draft.requested_ad_groups, MAX_GROUPS) if draft.requested_ad_groups else None
    if groups and len(draft.ad_groups) != groups:
        problems.append(
            f"it has {len(draft.ad_groups)} ad groups but exactly {groups} are required"
        )
    if draft.ad_count > MAX_TOTAL_ADS:
        problems.append(f"it has more than {MAX_TOTAL_ADS} ads in total")
    return "; ".join(problems)


def _count_warnings(draft: Draft) -> list[str]:
    notes: list[str] = []
    if draft.requested_ads and draft.requested_ads > MAX_TOTAL_ADS:
        notes.append(
            f"You asked for {draft.requested_ads} ads; one campaign draft holds at most "
            f"{MAX_TOTAL_ADS}. Launch this one, then ask for more in a second campaign."
        )
    elif draft.requested_ads and draft.ad_count != draft.requested_ads:
        notes.append(f"You asked for {draft.requested_ads} ads but the draft has {draft.ad_count}.")
    if draft.requested_ad_groups and len(draft.ad_groups) != draft.requested_ad_groups:
        notes.append(
            f"You asked for {draft.requested_ad_groups} ad groups but the draft has "
            f"{len(draft.ad_groups)}."
        )
    return notes


def _guardrail_warnings(workspace: Workspace, draft: Draft) -> list[str]:
    cap = Guardrail.objects.filter(
        workspace=workspace, key="max_single_spend_micros", enabled=True
    ).first()
    limit = int(cap.config.get("amount", 0) or 0) if cap else 0
    if limit and int(draft.daily_budget * MICROS) > limit:
        return [
            f"The daily budget is above your single-action spend cap of {limit / MICROS:.2f}; "
            "activation will be blocked unless the cap is raised."
        ]
    return []


def _site_warnings(draft: Draft, account: dict[str, Any]) -> list[str]:
    site = urlparse(str(account.get("url") or "")).hostname
    if not site:
        return []
    base = site.removeprefix("www.")
    off_site = {
        ad.url
        for group in draft.ad_groups
        for ad in group.ads
        if not (urlparse(ad.url).hostname or "").removeprefix("www.").endswith(base)
    }
    if not off_site:
        return []
    return [f"These links are not on your website ({site}): {', '.join(sorted(off_site))}."]


def suggest(
    workspace: Workspace,
    connection: Connection,
    service: LlmService,
    *,
    goal: str,
    previous: Draft | None = None,
    feedback: str = "",
) -> Suggestion:
    summary, account = _account_summary(connection)
    prompt = prompts.get_prompt(PROMPT_KEY)
    system = prompt.render(
        workspace_name=workspace.name,
        brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
        account_summary=summary,
        goal=goal,
        previous_draft=previous.model_dump_json(indent=1) if previous else "",
        feedback=feedback,
    )
    nudge = ""
    for _attempt in range(ATTEMPTS):
        request = LlmRequest(
            tier=prompt.tier,
            system=[SystemBlock(system)],
            messages=[Message.user("Answer with the structured suggestion." + nudge)],
        )
        result, _ = complete_structured(
            service,
            request,
            Suggestion,
            workspace_id=workspace.pk,
            prompt_key=f"{PROMPT_KEY}@v{prompt.version}",
        )
        problem = _count_problem(result.draft) if result.status == "draft" and result.draft else ""
        if not problem:
            break
        nudge = f"\nYour previous answer was rejected: {problem}. Fix the counts and answer again."
    if result.status == "draft" and result.draft is not None:
        draft = result.draft
        raised: list[str] = []
        if draft.daily_budget < MIN_DAILY_BUDGET:
            raised = [
                f"OpenAI Ads needs at least {MIN_DAILY_BUDGET:.0f} per day, so the budget was "
                f"raised from {draft.daily_budget:g} to {MIN_DAILY_BUDGET:.0f}."
            ]
            draft.daily_budget = MIN_DAILY_BUDGET
        countries = [code for code in draft.countries if COUNTRY_CODE.match(code)]
        draft.warnings = [
            *draft.warnings,
            *raised,
            *_count_warnings(draft),
            *_resolve_places(connection, draft.places, countries, "target"),
            *_resolve_places(connection, draft.excluded_places, countries, "excluded"),
            *_reach_warnings(draft),
            *_guardrail_warnings(workspace, draft),
            *_site_warnings(draft, account),
        ]
        result.questions = []
    elif result.status == "draft":
        result.status = "needs_input"
        result.questions = result.questions or [
            "Could you say more about what you want to promote?"
        ]
    return result


def launch_goal(connection_id: uuid.UUID, draft: Draft, image_url: str) -> str:
    image_rule = (
        f"Upload this image with openai_ads_upload_image and use the returned file id: {image_url}"
        if image_url
        else "No image was provided, so create the campaign and ad groups only and say that ads "
        "still need an image."
    )
    return (
        "The owner approved this draft in the OpenAI Ads screen. Create it exactly as written.\n"
        f"Connection id: {connection_id}\n\n"
        f"Approved draft (JSON):\n{json.dumps(draft.model_dump(), indent=1)}\n\n"
        "Rules:\n"
        "- Everything is created paused. Create the campaign first, then each ad group, then the "
        "ads, each in its own proposed action, waiting for the previous one to finish so you have "
        "its id.\n"
        f"- {image_rule}\n"
        "- Create the campaign with location_ids set to the id of every place in places that has "
        "one and excluded_location_ids from excluded_places; leave countries empty when "
        "location_ids are given. Pass description when the draft has one.\n"
        "- Create every ad group and every ad in the draft, so the counts match exactly.\n"
        "- Do not change any text, number or link in the draft. If a limit rejects something, "
        "stop and report which field and why.\n"
        "- Do not propose activation. The owner turns delivery on later from the same screen.\n"
        "- When finished, list what was created and what is waiting for approval."
    )


def launch(
    workspace: Workspace, connection: Connection, user_id: uuid.UUID, draft: Draft, image_url: str
) -> AgentRun:
    return runtime.start_run(
        workspace_id=workspace.pk,
        agent_name=AGENT,
        goal=launch_goal(connection.pk, draft, image_url),
        requested_by=f"user:{user_id}",
    )
