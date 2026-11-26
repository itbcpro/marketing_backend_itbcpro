"""AI optimisation of one Google Ads campaign: one reviewed plan, applied item by item.

The run gathers the campaign's structure, keywords, search terms, existing negatives, conversion
tracking and an audit of its landing pages, asks the model for findings and itemised changes, then
checks every item against the real data and Google's limits before a person ever sees it. Nothing
changes the ad account until the owner applies items, and each applied item is an ordinary action
(approval, guardrails, audit and rollback all apply).
"""

import json
import uuid
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone
from ninja import Schema
from pydantic import Field, ValidationError

from actions.models import ActionStatus, ProposedAction
from ads import providers
from ads.google import factory, landing, schemas
from ads.google.keywords import keyword_table
from ads.google.schemas import Keyword
from ads.models import AdEntity, AdLevel, AdOptimization, AdSyncState
from ads.openai import changes
from ads.openai.review import AGENT, container_snapshot
from ads.openai.stats import ad_ids, totals
from connections.models import Connection
from core.errors import ConflictError, DomainError, ErrorCode, NotFoundError, ValidationFailedError
from core.logging import get_logger
from identity.models import User
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import get_llm_service
from llm.structured import complete_structured
from memory import services as memory

log = get_logger(__name__)

PROMPT_KEY = "ads/google-optimize"
MAX_GOAL_CHARS = 1000
STUCK_AFTER = timedelta(minutes=12)
MICROS = 1_000_000
TERM_LIMIT = 300
MAX_BUDGET_SHIFT = 0.3
CALLOUT_CHARS = 25
RSA_HEADLINES = (3, 15)
NEGATIVES_QUERY = """
SELECT campaign_criterion.keyword.text, campaign_criterion.keyword.match_type
FROM campaign_criterion
WHERE campaign.id = {campaign} AND campaign_criterion.negative = TRUE
  AND campaign_criterion.type = 'KEYWORD'
"""
TERMS_QUERY = """
SELECT search_term_view.search_term, search_term_view.status, ad_group.id, metrics.impressions,
  metrics.clicks, metrics.cost_micros, metrics.conversions
FROM search_term_view
WHERE campaign.id = {campaign} AND segments.date DURING LAST_30_DAYS
ORDER BY metrics.cost_micros DESC LIMIT {limit}
"""


class Finding(Schema):
    area: Literal["tracking", "landing_page", "keywords", "ads", "budget", "structure"]
    severity: Literal["high", "medium", "low"]
    title: str = Field(max_length=120)
    detail: str = Field(max_length=600)


class Negative(Schema):
    text: str = Field(min_length=1, max_length=80)
    match_type: Literal["exact", "phrase"]
    scope: Literal["ad_group", "campaign"]
    ad_group_id: str = ""
    reason: str


class Addition(Schema):
    ad_group_id: str
    text: str = Field(min_length=1, max_length=80)
    match_type: Literal["exact", "phrase"]
    reason: str


class Pause(Schema):
    ad_group_id: str
    criterion_id: str
    text: str
    reason: str


class RsaRewrite(Schema):
    ad_id: str
    headlines: list[str]
    descriptions: list[str]
    reason: str


class NewAd(Schema):
    ad_group_id: str
    final_url: str
    headlines: list[str]
    descriptions: list[str]
    path1: str = ""
    path2: str = ""
    angle: str = ""
    reason: str


class Sitelink(Schema):
    link_text: str
    description1: str = ""
    description2: str = ""
    final_url: str


class Extensions(Schema):
    sitelinks: list[Sitelink] = Field(default_factory=list, max_length=8)
    callouts: list[str] = Field(default_factory=list, max_length=10)
    reason: str


class BudgetChange(Schema):
    daily_budget: float
    reason: str


class LandingTask(Schema):
    url: str
    problem: str
    fix: str
    priority: Literal["high", "medium", "low"]


class Plan(Schema):
    summary: str
    expected_effect: str
    findings: list[Finding] = Field(max_length=12)
    negatives: list[Negative] = Field(max_length=40)
    additions: list[Addition] = Field(max_length=25)
    pauses: list[Pause] = Field(max_length=20)
    rsa_rewrites: list[RsaRewrite] = Field(max_length=6)
    new_ads: list[NewAd] = Field(default_factory=list, max_length=6)
    extensions: Extensions | None = None
    budget_change: BudgetChange | None = None
    landing_tasks: list[LandingTask] = Field(max_length=10)
    notes: str = ""


def _enqueue(run_: AdOptimization) -> None:
    from ads.tasks import ad_optimize_task  # noqa: PLC0415 - the task module imports this one

    transaction.on_commit(lambda: ad_optimize_task.delay(str(run_.pk)))


def start(
    connection: Connection, user_id: uuid.UUID, campaign: AdEntity, goal: str
) -> AdOptimization:
    if connection.provider != providers.GOOGLE or campaign.level != AdLevel.CAMPAIGN:
        raise ValidationFailedError("optimisation belongs to a Google Ads campaign")
    text = goal.strip()
    if len(text) > MAX_GOAL_CHARS:
        raise ValidationFailedError("the request is too long")
    busy = AdOptimization.objects.filter(entity=campaign, status="thinking").first()
    if busy and busy.thinking_since and timezone.now() - busy.thinking_since < STUCK_AFTER:
        return busy
    run_ = AdOptimization.objects.create(
        workspace_id=connection.workspace_id,
        connection=connection,
        entity=campaign,
        created_by_id=user_id,
        goal=text,
        thinking_since=timezone.now(),
    )
    _enqueue(run_)
    return run_


def get(workspace_id: uuid.UUID, run_id: uuid.UUID) -> AdOptimization:
    found = AdOptimization.objects.filter(pk=run_id, workspace_id=workspace_id).first()
    if found is None:
        raise NotFoundError("optimisation not found")
    if (
        found.status == "thinking"
        and found.thinking_since
        and timezone.now() - found.thinking_since > STUCK_AFTER
    ):
        found.status = "failed"
        found.error_code = ErrorCode.PROVIDER_UNAVAILABLE.value
        found.save(update_fields=["status", "error_code", "updated_at"])
    return found


# --- gathering ------------------------------------------------------------------------------


def _groups(campaign: AdEntity) -> list[AdEntity]:
    return list(campaign.children.filter(level=AdLevel.AD_GROUP, missing_since__isnull=True))


def _ads(campaign: AdEntity) -> list[AdEntity]:
    return list(
        AdEntity.objects.filter(
            parent__parent=campaign, level=AdLevel.AD, missing_since__isnull=True
        )
    )


def _terms(connection: Connection, campaign_id: str) -> list[dict[str, Any]]:
    with factory.open_client(connection) as client:
        rows = client.search(TERMS_QUERY.format(campaign=int(campaign_id), limit=TERM_LIMIT))
    return [
        {
            "term": r.get("searchTermView", {}).get("searchTerm", ""),
            "ad_group_id": str(r.get("adGroup", {}).get("id", "")),
            "status": r.get("searchTermView", {}).get("status", ""),
            "impressions": int(r.get("metrics", {}).get("impressions") or 0),
            "clicks": int(r.get("metrics", {}).get("clicks") or 0),
            "cost": round(int(r.get("metrics", {}).get("costMicros") or 0) / MICROS, 2),
            "conversions": float(r.get("metrics", {}).get("conversions") or 0),
        }
        for r in rows
    ]


def _campaign_negatives(connection: Connection, campaign_id: str) -> list[dict[str, str]]:
    with factory.open_client(connection) as client:
        rows = client.search(NEGATIVES_QUERY.format(campaign=int(campaign_id)))
    return [
        {
            "text": str(r.get("campaignCriterion", {}).get("keyword", {}).get("text", "")),
            "match_type": str(
                r.get("campaignCriterion", {}).get("keyword", {}).get("matchType", "")
            ).lower(),
        }
        for r in rows
    ]


def _conversions(campaign: AdEntity) -> dict[str, Any]:
    state = AdSyncState.objects.filter(connection=campaign.connection_id).first()
    today = timezone.now().date()
    return {
        "conversion_actions": (state.account if state else {}).get("conversion_actions", []),
        "campaign_30_days": totals(ad_ids(campaign), today - timedelta(days=30), today),
    }


def structure_of(
    groups: list[AdEntity], ads: list[AdEntity], table: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Every ad group with its ads in full: ids, all text, strength and results."""
    today = timezone.now().date()
    by_group: dict[Any, list[AdEntity]] = {}
    for ad in ads:
        by_group.setdefault(ad.parent_id, []).append(ad)
    rows = []
    for group in groups:
        positive = [k for k in table.get(group.external_id, []) if not k.get("negative")]
        rows.append(
            {
                "ad_group_id": group.external_id,
                "name": group.name,
                "status": group.status,
                "max_cpc_micros": (group.data.get("bidding_config") or {}).get("max_bid_micros"),
                "keywords": [
                    {"text": k["text"], "match_type": k["match_type"], "clicks": k["clicks"]}
                    for k in sorted(positive, key=lambda k: -int(k.get("clicks") or 0))[:25]
                ],
                "keyword_count": len(positive),
                "ads": [_ad_view(ad, today) for ad in by_group.get(group.pk, [])],
            }
        )
    return rows


def _ad_view(ad: AdEntity, today: Any) -> dict[str, Any]:
    creative = ad.data.get("creative") or {}
    return {
        "ad_id": ad.external_id,
        "status": ad.status,
        "review_status": ad.review_status,
        "ad_strength": ad.data.get("ad_strength", ""),
        "headlines": creative.get("headlines") or [],
        "descriptions": creative.get("descriptions") or [],
        "pinned_headlines": creative.get("pinned_headlines") or {},
        "path1": creative.get("path1", ""),
        "path2": creative.get("path2", ""),
        "final_urls": creative.get("final_urls") or [],
        "last_30_days": totals([ad.pk], today - timedelta(days=30), today),
    }


def _public_landing(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the model reads: scores and facts without the raw page text kept for the screen."""
    return [
        {
            **p,
            "facts": {k: v for k, v in p.get("facts", {}).items() if k != "excerpt"},
            "excerpt": str(p.get("facts", {}).get("excerpt", ""))[:600],
        }
        for p in pages
    ]


# --- running --------------------------------------------------------------------------------


def run(run_id: uuid.UUID) -> str:
    row = (
        AdOptimization.objects.select_related("workspace", "connection", "entity")
        .filter(pk=run_id)
        .first()
    )
    if row is None or row.status != "thinking":
        return "skipped"
    campaign = row.entity
    try:
        groups = _groups(campaign)
        ads = _ads(campaign)
        pages = landing.audit_campaign(ads)
        terms = _terms(row.connection, campaign.external_id)
        negatives = _campaign_negatives(row.connection, campaign.external_id)
        table = {g.external_id: keyword_table(g) for g in groups}
        prompt = prompts.get_prompt(PROMPT_KEY)
        workspace = row.workspace
        system = prompt.render(
            workspace_name=workspace.name,
            brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
            memories=memory.render_memories(
                memory.retrieve(
                    workspace_id=workspace.pk,
                    query=f"google ads optimise {campaign.name}",
                    agent=AGENT,
                    limit=6,
                )
            ),
            snapshot=json.dumps(container_snapshot(campaign), indent=1, default=str),
            structure=json.dumps(structure_of(groups, ads, table), indent=1, default=str),
            keywords=json.dumps(table, indent=1, default=str),
            search_terms=json.dumps(terms, indent=1),
            campaign_negatives=json.dumps(negatives, indent=1),
            conversions=json.dumps(_conversions(campaign), indent=1, default=str),
            landing=json.dumps(_public_landing(pages), indent=1, default=str),
            goal=row.goal,
        )
        request = LlmRequest(
            tier=prompt.tier,
            system=[SystemBlock(system)],
            messages=[Message.user("Answer with the optimisation plan.")],
        )
        result, _ = complete_structured(
            get_llm_service(workspace.pk),
            request,
            Plan,
            workspace_id=workspace.pk,
            prompt_key=f"{PROMPT_KEY}@v{prompt.version}",
        )
    except DomainError as exc:
        return _fail(row, exc.code.value)
    except Exception:
        log.exception("ad_optimize_crashed")
        return _fail(row, ErrorCode.UNKNOWN.value)
    row.landing = pages
    row.plan = checked_plan(result, campaign, groups, ads, table, negatives, pages)
    row.status = "done"
    row.thinking_since = None
    row.save()
    return "done"


def _fail(row: AdOptimization, code: str) -> str:
    row.status = "failed"
    row.error_code = code
    row.thinking_since = None
    row.save(update_fields=["status", "error_code", "thinking_since", "updated_at"])
    return "failed"


# --- checking the model's items against the real data --------------------------------------


def _keyword_ok(text: str, match_type: str) -> Keyword | None:
    try:
        return Keyword(text=text, match_type=match_type)  # type: ignore[arg-type]
    except ValidationError:
        return None


def checked_plan(
    result: Plan,
    campaign: AdEntity,
    groups: list[AdEntity],
    ads: list[AdEntity],
    table: dict[str, list[dict[str, Any]]],
    existing_negatives: list[dict[str, str]],
    pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep only items that are valid, new and point at real things; say what was dropped."""
    group_ids = {g.external_id for g in groups}
    dropped: list[str] = []
    have_negative = {n["text"].lower() for n in existing_negatives} | {
        str(k["text"]).lower() for rows in table.values() for k in rows if k.get("negative")
    }
    have_keyword = {
        (gid, str(k["text"]).lower())
        for gid, rows in table.items()
        for k in rows
        if not k.get("negative")
    }
    negatives: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in result.negatives:
        clean = _keyword_ok(n.text, n.match_type)
        group = n.ad_group_id if n.ad_group_id in group_ids else ""
        if clean is None or clean.text.lower() in have_negative | seen:
            dropped.append(f"negative: {n.text}")
            continue
        if n.scope == "ad_group" and not group:
            n.scope = "campaign"  # no valid group named: the safe reading is campaign wide
        seen.add(clean.text.lower())
        negatives.append({**n.model_dump(), "text": clean.text, "ad_group_id": group})
    additions = []
    for a in result.additions:
        clean = _keyword_ok(a.text, a.match_type)
        if (
            clean is None
            or a.ad_group_id not in group_ids
            or (
                a.ad_group_id,
                clean.text.lower(),
            )
            in have_keyword
        ):
            dropped.append(f"keyword: {a.text}")
            continue
        additions.append({**a.model_dump(), "text": clean.text})
    live = {
        (gid, str(k["criterion_id"]))
        for gid, rows in table.items()
        for k in rows
        if not k["negative"]
    }
    pauses = [p.model_dump() for p in result.pauses if (p.ad_group_id, p.criterion_id) in live]
    rsa_ids = {a.external_id for a in ads if (a.data.get("creative") or {}).get("headlines")}
    rewrites = []
    for r in result.rsa_rewrites:
        try:
            schemas.UpdateRsa(
                connection_id=uuid.uuid4(), ad_id=r.ad_id, headlines=r.headlines,
                descriptions=r.descriptions,
            )  # fmt: skip
        except ValidationError:
            dropped.append(f"ad text: {r.ad_id}")
            continue
        if r.ad_id in rsa_ids:
            rewrites.append(r.model_dump())
        else:
            dropped.append(f"ad text: {r.ad_id}")
    new_ads = _checked_new_ads(result.new_ads, group_ids, ads, dropped)
    extensions = _checked_extensions(result.extensions, ads, dropped)
    budget = _checked_budget(result.budget_change, campaign, dropped)
    urls = {p["url"] for p in pages}
    tasks = [t.model_dump() for t in result.landing_tasks if t.url in urls or not urls]
    return {
        "summary": result.summary,
        "expected_effect": result.expected_effect,
        "findings": [f.model_dump() for f in result.findings],
        "negatives": negatives,
        "additions": additions,
        "pauses": pauses,
        "rsa_rewrites": rewrites,
        "new_ads": new_ads,
        "extensions": extensions,
        "budget_change": budget,
        "landing_tasks": tasks,
        "notes": result.notes,
        "dropped": dropped,
    }


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _urls_of(ads: list[AdEntity]) -> set[str]:
    return {
        str(url) for ad in ads for url in ((ad.data.get("creative") or {}).get("final_urls") or [])
    }


def _checked_new_ads(
    items: list[NewAd], group_ids: set[str], ads: list[AdEntity], dropped: list[str]
) -> list[dict[str, Any]]:
    """New ads need a real group, a URL the campaign already uses and RSA-sized text."""
    known = _urls_of(ads)
    kept: list[dict[str, Any]] = []
    for item in items:
        label = f"new ad: {item.angle or item.ad_group_id}"
        try:
            schemas.AddRsa(
                connection_id=uuid.uuid4(), ad_group_id=item.ad_group_id, final_url=item.final_url,
                headlines=item.headlines, descriptions=item.descriptions, path1=item.path1,
                path2=item.path2,
            )  # fmt: skip
        except ValidationError:
            dropped.append(label)
            continue
        if item.ad_group_id not in group_ids or item.final_url not in known:
            dropped.append(label)
            continue
        kept.append(item.model_dump())
    return kept


def _checked_extensions(
    item: Extensions | None, ads: list[AdEntity], dropped: list[str]
) -> dict[str, Any] | None:
    """Sitelinks must stay on the site the ads already send people to."""
    if item is None:
        return None
    hosts = {_host(url) for url in _urls_of(ads)}
    links: list[dict[str, Any]] = []
    for link in item.sitelinks:
        try:
            schemas.Sitelink(**link.model_dump())
        except ValidationError:
            dropped.append(f"sitelink: {link.link_text}")
            continue
        if _host(link.final_url) in hosts:
            links.append(link.model_dump())
        else:
            dropped.append(f"sitelink: {link.link_text}")
    callouts = []
    for text in dict.fromkeys(c.strip() for c in item.callouts):
        if 0 < len(text) <= CALLOUT_CHARS:
            callouts.append(text)
        else:
            dropped.append(f"callout: {text[:30]}")
    if not links and not callouts:
        return None
    return {"sitelinks": links, "callouts": callouts, "reason": item.reason}


def _checked_budget(
    change: BudgetChange | None, campaign: AdEntity, dropped: list[str]
) -> dict[str, Any] | None:
    if change is None:
        return None
    current = (campaign.data.get("budget") or {}).get("daily_spend_limit_micros")
    if not current or campaign.data.get("budget_shared"):
        dropped.append("budget: this campaign has no daily budget of its own")
        return None
    target = round(change.daily_budget * MICROS)
    low, high = current * (1 - MAX_BUDGET_SHIFT), current * (1 + MAX_BUDGET_SHIFT)
    if not low <= target <= high or target < schemas.MIN_BUDGET_MICROS or target == current:
        dropped.append("budget: outside the allowed range")
        return None
    return {"from_micros": int(current), "to_micros": target, "reason": change.reason}


# --- applying -------------------------------------------------------------------------------

FAILED = {ActionStatus.FAILED_FINAL.value, ActionStatus.REJECTED.value, ActionStatus.EXPIRED.value}
EXPLAIN_MAX = 300


def item_states(row: AdOptimization) -> dict[str, dict[str, Any]]:
    """What became of each applied item: waiting, done in Google Ads, or failed and why."""
    ids = {v["action_id"] for v in row.applied.values()}
    actions = {
        str(a.pk): a
        for a in ProposedAction.objects.filter(workspace_id=row.workspace_id, pk__in=ids)
    }
    states: dict[str, dict[str, Any]] = {}
    for key, value in row.applied.items():
        action = actions.get(value["action_id"])
        states[key] = {
            "action_id": value["action_id"],
            "status": action.status if action else "unknown",
            "error": (action.error or "")[:EXPLAIN_MAX] if action else "",
        }
    return states


def apply(
    row: AdOptimization, user: User, keys: list[str], *, apply_now: bool
) -> tuple[AdOptimization, dict[str, dict[str, Any]]]:
    """Turn the chosen items into actions. ``apply_now`` approves them as this user when allowed."""
    if row.status != "done" or not row.plan:
        raise ValidationFailedError("there is no plan to apply")
    plan: dict[str, Any] = row.plan
    states = item_states(row)
    retry = {k for k, s in states.items() if s["status"] in FAILED}
    done: dict[str, Any] = {k: v for k, v in row.applied.items() if k not in retry}
    wanted = [k for k in dict.fromkeys(keys) if k not in done]
    if not wanted:
        raise ConflictError("those items were already applied, or none was chosen")
    campaign = row.entity
    results: dict[str, dict[str, Any]] = {}

    def submit(kind: str, payload: dict[str, Any], summary: str, member_keys: list[str]) -> None:
        # A retried item must be a new action, not the old one the idempotency key returns.
        attempt = 1 + max(
            (row.applied.get(k, {}).get("attempt", 0) for k in member_keys), default=0
        )
        action, ok = changes.submit(
            row.connection,
            user,
            kind,
            payload,
            summary=summary[:300],
            apply_now=apply_now,
            idempotency_key=f"optimize:{row.pk}:{','.join(sorted(member_keys))}:{attempt}",
        )
        for key in member_keys:
            results[key] = {"action_id": str(action.pk), "applied": ok, "attempt": attempt}

    def pick(prefix: str, items: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        return [(f"{prefix}:{i}", items[i]) for i in range(len(items)) if f"{prefix}:{i}" in wanted]

    _submit_negatives(pick("neg", plan["negatives"]), campaign, submit)
    _submit_additions(pick("add", plan["additions"]), submit)
    _submit_singles(plan, wanted, campaign, submit, pick)
    if not results:
        raise ValidationFailedError("choose at least one item")
    row.applied = {**done, **results}
    row.save(update_fields=["applied", "updated_at"])
    return row, results


def _submit_singles(
    plan: dict[str, Any], wanted: list[str], campaign: AdEntity, submit: Any, pick: Any
) -> None:
    """Items that become one action each: pauses, ad text, new ads, extensions and budget."""
    for key, item in pick("pause", plan["pauses"]):
        submit(
            "keyword.pause",
            {"ad_group_id": item["ad_group_id"], "criterion_id": item["criterion_id"]},
            f"Pause keyword: {item['text']}",
            [key],
        )
    for key, item in pick("rsa", plan["rsa_rewrites"]):
        submit(
            "ad.update_rsa",
            {
                "ad_id": item["ad_id"],
                "headlines": item["headlines"],
                "descriptions": item["descriptions"],
            },
            f"Replace ad text of ad {item['ad_id']}",
            [key],
        )
    for key, item in pick("newad", plan.get("new_ads", [])):
        submit(
            "ad.add_rsa",
            {
                "ad_group_id": item["ad_group_id"],
                "final_url": item["final_url"],
                "headlines": item["headlines"],
                "descriptions": item["descriptions"],
                "path1": item.get("path1", ""),
                "path2": item.get("path2", ""),
            },
            f"Add a new ad to ad group {item['ad_group_id']}: {item.get('angle', '')}",
            [key],
        )
    extensions = plan.get("extensions")
    if extensions and "ext" in wanted:
        submit(
            "campaign.add_extensions",
            {
                "campaign_id": campaign.external_id,
                "sitelinks": extensions["sitelinks"],
                "callouts": extensions["callouts"],
            },
            "Add sitelinks and callouts to the campaign",
            ["ext"],
        )
    budget = plan.get("budget_change")
    if budget and "budget" in wanted:
        submit(
            "campaign.update_budget",
            {"campaign_id": campaign.external_id, "daily_spend_limit_micros": budget["to_micros"]},
            "Change the campaign daily budget",
            ["budget"],
        )


def _submit_negatives(
    chosen: list[tuple[str, dict[str, Any]]], campaign: AdEntity, submit: Any
) -> None:
    by_parent: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    for key, item in chosen:
        scope = (
            item["scope"] if item.get("ad_group_id") or item["scope"] == "campaign" else "campaign"
        )
        parent = campaign.external_id if scope == "campaign" else item["ad_group_id"]
        by_parent.setdefault((scope, parent), []).append((key, item))
    for (scope, parent), members in by_parent.items():
        submit(
            "negatives.add",
            {
                "scope": scope,
                "parent_id": parent,
                "keywords": [
                    {"text": m["text"], "match_type": m["match_type"]} for _, m in members
                ],
            },
            f"Add {len(members)} negative keywords ({scope})",
            [k for k, _ in members],
        )


def _submit_additions(chosen: list[tuple[str, dict[str, Any]]], submit: Any) -> None:
    by_group: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for key, item in chosen:
        by_group.setdefault(item["ad_group_id"], []).append((key, item))
    for group, members in by_group.items():
        submit(
            "keywords.add",
            {
                "ad_group_id": group,
                "keywords": [
                    {"text": m["text"], "match_type": m["match_type"]} for _, m in members
                ],
            },
            f"Add {len(members)} keywords",
            [k for k, _ in members],
        )
