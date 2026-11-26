"""AI keyword suggestions for a Google Ads ad group, built from its real search terms."""

import json
import uuid
from datetime import timedelta
from typing import Any, Literal

from django.db import transaction
from django.utils import timezone
from ninja import Schema
from pydantic import Field

from ads import providers
from ads.google import factory
from ads.google.schemas import Keyword
from ads.models import AdEntity, AdKeywordProposal, AdLevel
from ads.openai import changes
from ads.openai.review import AGENT, container_snapshot
from ads.openai.stats import totals
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

PROMPT_KEY = "ads/google-keywords"
MAX_INSTRUCTION_CHARS = 1000
STUCK_AFTER = timedelta(minutes=10)
MICROS = 1_000_000
TERM_LIMIT = 300
MAX_KEYWORDS_SHOWN = 300
SEARCH_TERMS = """
SELECT search_term_view.search_term, search_term_view.status, metrics.impressions,
  metrics.clicks, metrics.cost_micros, metrics.conversions
FROM search_term_view
WHERE ad_group.id = {ad_group} AND segments.date DURING LAST_30_DAYS
ORDER BY metrics.cost_micros DESC LIMIT {limit}
"""


class Negative(Schema):
    text: str = Field(min_length=1, max_length=80)
    match_type: Literal["exact", "phrase"]
    scope: Literal["ad_group", "campaign"]
    reason: str


class Addition(Schema):
    text: str = Field(min_length=1, max_length=80)
    match_type: Literal["exact", "phrase"]
    reason: str


class Pause(Schema):
    criterion_id: str
    text: str
    reason: str


class KeywordResult(Schema):
    negatives: list[Negative] = Field(max_length=30)
    additions: list[Addition] = Field(max_length=20)
    pauses: list[Pause] = Field(max_length=15)
    summary: str
    notes: str


def _enqueue(proposal: AdKeywordProposal) -> None:
    from ads.tasks import ad_keywords_task

    transaction.on_commit(lambda: ad_keywords_task.delay(str(proposal.pk)))


def start(
    connection: Connection, user_id: uuid.UUID, entity: AdEntity, instruction: str
) -> AdKeywordProposal:
    if connection.provider != providers.GOOGLE or entity.level != AdLevel.AD_GROUP:
        raise ValidationFailedError("keyword suggestions belong to a Google Ads ad group")
    text = instruction.strip()
    if len(text) > MAX_INSTRUCTION_CHARS:
        raise ValidationFailedError("the request is too long")
    busy = AdKeywordProposal.objects.filter(entity=entity, status="thinking").first()
    if busy and busy.thinking_since and timezone.now() - busy.thinking_since < STUCK_AFTER:
        return busy
    proposal = AdKeywordProposal.objects.create(
        workspace_id=connection.workspace_id,
        connection=connection,
        entity=entity,
        created_by_id=user_id,
        instruction=text,
        thinking_since=timezone.now(),
    )
    _enqueue(proposal)
    return proposal


def get(workspace_id: uuid.UUID, proposal_id: uuid.UUID) -> AdKeywordProposal:
    found = AdKeywordProposal.objects.filter(pk=proposal_id, workspace_id=workspace_id).first()
    if found is None:
        raise NotFoundError("keyword suggestions not found")
    if (
        found.status == "thinking"
        and found.thinking_since
        and timezone.now() - found.thinking_since > STUCK_AFTER
    ):
        found.status = "failed"
        found.error_code = ErrorCode.PROVIDER_UNAVAILABLE.value
        found.save(update_fields=["status", "error_code", "updated_at"])
    return found


def _fetch_terms(connection: Connection, ad_group_id: str) -> list[dict[str, Any]]:
    query = SEARCH_TERMS.format(ad_group=int(ad_group_id), limit=TERM_LIMIT)
    with factory.open_client(connection) as client:
        rows = client.search(query)
    return [
        {
            "term": r.get("searchTermView", {}).get("searchTerm", ""),
            "status": r.get("searchTermView", {}).get("status", ""),
            "impressions": int(r.get("metrics", {}).get("impressions") or 0),
            "clicks": int(r.get("metrics", {}).get("clicks") or 0),
            "cost": round(int(r.get("metrics", {}).get("costMicros") or 0) / MICROS, 2),
            "conversions": float(r.get("metrics", {}).get("conversions") or 0),
        }
        for r in rows
    ]


def keyword_table(group: AdEntity) -> list[dict[str, Any]]:
    today = timezone.now().date()
    table = []
    for kw in group.children.filter(level=AdLevel.KEYWORD, missing_since__isnull=True)[
        :MAX_KEYWORDS_SHOWN
    ]:
        stats = totals([kw.pk], today - timedelta(days=30), today)
        table.append(
            {
                "criterion_id": kw.data.get("criterion_id"),
                "text": kw.name,
                "match_type": kw.data.get("match_type"),
                "negative": kw.data.get("negative"),
                "status": kw.status,
                "quality_score": kw.data.get("quality_score"),
                "clicks": int(stats["clicks"]),
                "cost": stats["spend"],
                "conversions": stats["conversions"],
            }
        )
    return table


def run(proposal_id: uuid.UUID) -> str:
    proposal = (
        AdKeywordProposal.objects.select_related("workspace", "connection", "entity__parent")
        .filter(pk=proposal_id)
        .first()
    )
    if proposal is None or proposal.status != "thinking":
        return "skipped"
    group = proposal.entity
    try:
        terms = _fetch_terms(proposal.connection, group.external_id)
        keywords = keyword_table(group)
        prompt = prompts.get_prompt(PROMPT_KEY)
        workspace = proposal.workspace
        system = prompt.render(
            workspace_name=workspace.name,
            brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
            memories=memory.render_memories(
                memory.retrieve(
                    workspace_id=workspace.pk,
                    query=f"keywords {group.name}",
                    agent=AGENT,
                    limit=6,
                )
            ),
            snapshot=json.dumps(container_snapshot(group), indent=1, default=str),
            search_terms=json.dumps(terms, indent=1),
            keywords=json.dumps(keywords, indent=1),
            instruction=proposal.instruction,
        )
        request = LlmRequest(
            tier=prompt.tier,
            system=[SystemBlock(system)],
            messages=[Message.user("Answer with the keyword suggestions.")],
        )
        result, _ = complete_structured(
            get_llm_service(workspace.pk),
            request,
            KeywordResult,
            workspace_id=workspace.pk,
            prompt_key=f"{PROMPT_KEY}@v{prompt.version}",
        )
    except DomainError as exc:
        return _fail(proposal, exc.code.value)
    except Exception:
        log.exception("ad_keywords_crashed")
        return _fail(proposal, ErrorCode.UNKNOWN.value)
    known = {str(k["criterion_id"]) for k in keywords}
    existing = {str(k["text"]).lower() for k in keywords if not k["negative"]}
    proposal.search_terms = terms[:50]
    proposal.negatives = [n.model_dump() for n in result.negatives]
    proposal.additions = [
        a.model_dump() for a in result.additions if a.text.lower() not in existing
    ]
    proposal.pauses = [p.model_dump() for p in result.pauses if p.criterion_id in known]
    proposal.summary, proposal.notes = result.summary, result.notes
    proposal.status = "done"
    proposal.thinking_since = None
    proposal.save()
    return "done"


def _fail(proposal: AdKeywordProposal, code: str) -> str:
    proposal.status = "failed"
    proposal.error_code = code
    proposal.thinking_since = None
    proposal.save(update_fields=["status", "error_code", "thinking_since", "updated_at"])
    return "failed"


def _clean(item: dict[str, Any]) -> Keyword:
    return Keyword(text=item["text"], match_type=item["match_type"])


def apply(
    proposal: AdKeywordProposal,
    user: User,
    *,
    negatives: list[int],
    additions: list[int],
    pauses: list[int],
) -> tuple[AdKeywordProposal, dict[str, dict[str, Any]]]:
    """Turn the chosen suggestions into actions (one per kind), applied now if the user may."""
    if proposal.status != "done":
        raise ValidationFailedError("there are no suggestions to apply")
    if proposal.applied:
        raise ConflictError("these suggestions were already applied")
    group = proposal.entity
    campaign_id = group.parent.external_id if group.parent else ""
    results: dict[str, dict[str, Any]] = {}

    def submit(kind: str, payload: dict[str, Any], summary: str, key: str) -> None:
        action, done = changes.submit(
            proposal.connection, user, kind, payload, summary=summary, apply_now=True
        )
        results[key] = {"action_id": str(action.pk), "applied": done}

    chosen_neg = [proposal.negatives[i] for i in negatives if 0 <= i < len(proposal.negatives)]
    for scope, parent in (("ad_group", group.external_id), ("campaign", campaign_id)):
        items = [n for n in chosen_neg if n["scope"] == scope]
        if items:
            submit(
                "negatives.add",
                {
                    "scope": scope,
                    "parent_id": parent,
                    "keywords": [_clean(n).model_dump() for n in items],
                },
                f"Add {len(items)} negative keywords ({scope})",
                f"negatives_{scope}",
            )
    chosen_add = [proposal.additions[i] for i in additions if 0 <= i < len(proposal.additions)]
    if chosen_add:
        submit(
            "keywords.add",
            {
                "ad_group_id": group.external_id,
                "keywords": [_clean(a).model_dump() for a in chosen_add],
            },
            f"Add {len(chosen_add)} keywords",
            "additions",
        )
    for index in pauses:
        if 0 <= index < len(proposal.pauses):
            item = proposal.pauses[index]
            submit(
                "keyword.pause",
                {"ad_group_id": group.external_id, "criterion_id": str(item["criterion_id"])},
                f"Pause keyword: {item['text']}"[:300],
                f"pause_{item['criterion_id']}",
            )
    if not results:
        raise ValidationFailedError("choose at least one suggestion")
    proposal.applied = results
    proposal.save()
    return proposal, results
