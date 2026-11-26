"""AI reviews of one ad or of the whole account, compared with the previous review.

Each review stores the state it looked at (snapshot) and, for the next one, what changed since and
how the numbers moved, so regressions after a change can be found instead of guessed.
"""

import json
import uuid
from datetime import date, timedelta
from typing import Any, Literal

from django.db import transaction
from django.utils import timezone
from ninja import Schema
from pydantic import Field

from ads import providers
from ads.models import (
    AdChange,
    AdEntity,
    AdLevel,
    AdReview,
    AdSyncState,
    ReviewScope,
)
from ads.openai.stats import ad_ids, series, totals
from ads.syncing import TRACKED
from connections.models import Connection
from core.errors import ConflictError, DomainError, ErrorCode, NotFoundError
from core.logging import get_logger
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import get_llm_service
from llm.structured import complete_structured
from memory import services as memory
from workspaces.models import Workspace

log = get_logger(__name__)

PROMPT_KEY = "ads/openai-review"
AGENT = "ads-strategist"
MICROS = 1_000_000
WINDOW_DAYS = 7
SERIES_DAYS = 14
STUCK_AFTER = timedelta(minutes=15)
MAX_ACCOUNT_ADS = 40


class Fix(Schema):
    """A change Claude proposes for one problem; the owner decides whether to apply it."""

    type: Literal["ad_text", "context_hints", "max_bid", "pause_ad"]
    new_title: str | None
    new_body: str | None
    new_hints: list[str] | None
    new_max_bid: float | None


class Issue(Schema):
    severity: Literal["high", "medium", "low"]
    area: Literal[
        "creative", "targeting", "budget_bidding", "delivery", "policy", "tracking", "results"
    ]
    problem: str
    evidence: str
    suggestion: str
    fix: Fix | None


class SinceLast(Schema):
    assessment: Literal["first_review", "improved", "unchanged", "worse", "not_enough_data"]
    explanation: str
    regressions: list[str]


class ReviewResult(Schema):
    verdict: Literal["healthy", "needs_attention", "critical"]
    score: int = Field(ge=0, le=100)
    summary: str
    strengths: list[str]
    issues: list[Issue]
    since_last_review: SinceLast
    next_steps: list[str]


def _view(entity: AdEntity) -> dict[str, Any]:
    view = {k: entity.data.get(k) for k in TRACKED[entity.level]}
    return {"id": entity.external_id, "level": entity.level, **view}


# --- snapshots --------------------------------------------------------------------------------


def ad_snapshot(entity: AdEntity) -> dict[str, Any]:
    group = entity.parent
    campaign = group.parent if group else None
    today = timezone.now().date()
    ids = [entity.pk]
    state = AdSyncState.objects.filter(connection=entity.connection_id).first()
    return {
        "as_of": today.isoformat(),
        "data_synced_at": state.last_synced_at.isoformat()
        if state and state.last_synced_at
        else None,
        "platform_diagnostics": providers.for_entity(entity).platform_view(entity),
        "automatic_checks": providers.for_entity(entity).run_checks(
            entity, state.account if state else {}
        ),
        "ad": _view(entity),
        "ad_group": _view(group) if group else None,
        "campaign": _view(campaign) if campaign else None,
        "siblings": [
            {"name": s.name, "status": s.status, "review_status": s.review_status}
            for s in (group.children.exclude(pk=entity.pk) if group else [])
        ],
        "metrics": {
            "last_7_days": totals(ids, today - timedelta(days=WINDOW_DAYS), today),
            "last_30_days": totals(ids, today - timedelta(days=30), today),
            "daily": series(ids, SERIES_DAYS),
        },
    }


def _child_view(kid: AdEntity, today: date, *, nested: bool = True) -> dict[str, Any]:
    """One ad group or ad as the reviewer sees it, with its wording, settings and numbers."""
    kid_ids = ad_ids(kid)
    row: dict[str, Any] = {
        "level": kid.level,
        "name": kid.name,
        "status": kid.status,
        "review_status": kid.review_status,
        "last_7_days": totals(kid_ids, today - timedelta(days=WINDOW_DAYS), today),
        "last_30_days": totals(kid_ids, today - timedelta(days=30), today),
    }
    if kid.level == AdLevel.AD:
        creative = kid.data.get("creative") or {}
        row["headline"] = creative.get("title")
        row["text"] = creative.get("body")
        row["link"] = creative.get("target_url")
    else:
        row["context_hints"] = kid.data.get("context_hints") or []
        row["bidding"] = kid.data.get("bidding_config")
        row["bid_too_low"] = bool(kid.data.get("bid_too_low"))
        if nested:
            row["ads"] = [
                _child_view(ad, today, nested=False)
                for ad in kid.children.filter(missing_since__isnull=True)[:MAX_ACCOUNT_ADS]
            ]
    return row


def container_snapshot(entity: AdEntity) -> dict[str, Any]:
    """Snapshot of an ad group or a campaign: its own settings plus all ads beneath it."""
    today = timezone.now().date()
    ids = ad_ids(entity)
    state = AdSyncState.objects.filter(connection=entity.connection_id).first()
    kids = list(entity.children.filter(missing_since__isnull=True)[:MAX_ACCOUNT_ADS])
    kid_rows = [_child_view(kid, today) for kid in kids]
    return {
        "as_of": today.isoformat(),
        "data_synced_at": state.last_synced_at.isoformat()
        if state and state.last_synced_at
        else None,
        "platform_diagnostics": providers.for_entity(entity).platform_view(entity),
        "automatic_checks": providers.for_entity(entity).run_checks(
            entity, state.account if state else {}
        ),
        "entity": _view(entity),
        "parent": _view(entity.parent) if entity.parent else None,
        "children": kid_rows,
        "metrics": {
            "last_7_days": totals(ids, today - timedelta(days=WINDOW_DAYS), today),
            "last_30_days": totals(ids, today - timedelta(days=30), today),
            "daily": series(ids, SERIES_DAYS),
        },
    }


def entity_snapshot(entity: AdEntity) -> dict[str, Any]:
    return ad_snapshot(entity) if entity.level == AdLevel.AD else container_snapshot(entity)


def account_snapshot(connection: Connection) -> dict[str, Any]:
    today = timezone.now().date()
    ads = list(
        AdEntity.objects.filter(
            connection=connection, level=AdLevel.AD, missing_since__isnull=True
        ).select_related("parent__parent")[:MAX_ACCOUNT_ADS]
    )
    state = AdSyncState.objects.filter(connection=connection).first()
    rows = []
    for ad in ads:
        latest = AdReview.objects.filter(entity=ad, status="done").order_by("-created_at").first()
        rows.append(
            {
                "name": ad.name,
                "status": ad.status,
                "review_status": ad.review_status,
                "campaign": ad.parent.parent.name if ad.parent and ad.parent.parent else "",
                "ad_group": ad.parent.name if ad.parent else "",
                "last_7_days": totals([ad.pk], today - timedelta(days=WINDOW_DAYS), today),
                "last_ai_verdict": (latest.result or {}).get("verdict") if latest else None,
            }
        )
    all_ids = [a.pk for a in ads]
    return {
        "as_of": today.isoformat(),
        "data_synced_at": state.last_synced_at.isoformat()
        if state and state.last_synced_at
        else None,
        "ads": rows,
        "metrics": {
            "last_7_days": totals(all_ids, today - timedelta(days=WINDOW_DAYS), today),
            "last_30_days": totals(all_ids, today - timedelta(days=30), today),
            "daily": series(all_ids, SERIES_DAYS),
        },
    }


def compare(
    previous: AdReview | None, snapshot: dict[str, Any], entity_ids: list[uuid.UUID]
) -> dict[str, Any] | None:
    """What changed since the previous review and how the numbers moved."""
    if previous is None:
        return None
    changes = AdChange.objects.filter(entity__in=_related(entity_ids), at__gt=previous.created_at)
    now = snapshot["metrics"]["last_7_days"]
    before = (previous.snapshot or {}).get("metrics", {}).get("last_7_days", {})
    moved = {
        key: {
            "before": before.get(key),
            "after": now.get(key),
            "change_pct": round((now[key] - before[key]) / before[key] * 100, 1)
            if before.get(key)
            else None,
        }
        for key in ("impressions", "clicks", "spend", "ctr", "cpc")
    }
    return {
        "previous_review_at": previous.created_at.isoformat(),
        "days_since": (timezone.now() - previous.created_at).days,
        "changes": [
            {
                "at": c.at.isoformat(),
                "what": c.entity.name,
                "level": c.entity.level,
                "diff": c.diff,
            }
            for c in changes.select_related("entity")[:40]
        ],
        "metrics_7d": moved,
    }


def _related(ad_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """The ads plus their ad groups and campaigns, whose edits also affect them."""
    ads = AdEntity.objects.filter(pk__in=ad_ids).select_related("parent__parent")
    ids: set[uuid.UUID] = set(ad_ids)
    for ad in ads:
        if ad.parent:
            ids.add(ad.parent.pk)
            if ad.parent.parent:
                ids.add(ad.parent.parent.pk)
    return list(ids)


# --- lifecycle --------------------------------------------------------------------------------


SCOPE_BY_LEVEL = {
    AdLevel.AD: ReviewScope.AD,
    AdLevel.AD_GROUP: ReviewScope.AD_GROUP,
    AdLevel.CAMPAIGN: ReviewScope.CAMPAIGN,
}
SCOPE_TEXT = {
    ReviewScope.AD: "one ad",
    ReviewScope.AD_GROUP: "one ad group, looking at all of its ads together",
    ReviewScope.CAMPAIGN: "one campaign, looking at all of its ad groups and ads together",
}


def _enqueue(review: AdReview) -> None:
    from ads.tasks import ad_review_task

    transaction.on_commit(lambda: ad_review_task.delay(str(review.pk)))


def start(
    connection: Connection, user_id: uuid.UUID, *, entity: AdEntity | None = None
) -> AdReview:
    scope = SCOPE_BY_LEVEL[AdLevel(entity.level)] if entity else ReviewScope.ACCOUNT
    if not AdSyncState.objects.filter(connection=connection, last_synced_at__isnull=False).exists():
        raise ConflictError("sync the account before reviewing it")
    busy = AdReview.objects.filter(connection=connection, entity=entity, status="thinking").first()
    if busy and busy.thinking_since and timezone.now() - busy.thinking_since < STUCK_AFTER:
        return busy
    review = AdReview.objects.create(
        workspace_id=connection.workspace_id,
        connection=connection,
        scope=scope,
        entity=entity,
        created_by_id=user_id,
        thinking_since=timezone.now(),
    )
    _enqueue(review)
    return review


def get(workspace_id: uuid.UUID, review_id: uuid.UUID) -> AdReview:
    review = AdReview.objects.filter(pk=review_id, workspace_id=workspace_id).first()
    if review is None:
        raise NotFoundError("review not found")
    if (
        review.status == "thinking"
        and review.thinking_since
        and timezone.now() - review.thinking_since > STUCK_AFTER
    ):
        review.status = "failed"
        review.error_code = ErrorCode.PROVIDER_UNAVAILABLE.value
        review.save(update_fields=["status", "error_code", "updated_at"])
    return review


def _previous(review: AdReview) -> AdReview | None:
    return (
        AdReview.objects.filter(
            connection=review.connection_id,
            scope=review.scope,
            entity=review.entity,
            status="done",
            created_at__lt=review.created_at,
        )
        .order_by("-created_at")
        .first()
    )


def run_review(review_id: uuid.UUID) -> str:
    review = (
        AdReview.objects.select_related("workspace", "connection", "entity__parent__parent")
        .filter(pk=review_id)
        .first()
    )
    if review is None or review.status != "thinking":
        return "skipped"
    try:
        snapshot = (
            entity_snapshot(review.entity) if review.entity else account_snapshot(review.connection)
        )
        ids = (
            ad_ids(review.entity)
            if review.entity
            else [
                e.pk
                for e in AdEntity.objects.filter(
                    connection=review.connection, level=AdLevel.AD, missing_since__isnull=True
                )
            ]
        )
        previous = _previous(review)
        comparison = compare(previous, snapshot, ids)
        prompt = prompts.get_prompt(PROMPT_KEY)
        platform = providers.for_connection(review.connection)
        workspace: Workspace = review.workspace
        name = review.entity.name if review.entity else "account"
        system = prompt.render(
            workspace_name=workspace.name,
            brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
            memories=memory.render_memories(
                memory.retrieve(
                    workspace_id=workspace.pk, query=f"ads {name}", agent=AGENT, limit=6
                )
            ),
            scope=SCOPE_TEXT.get(ReviewScope(review.scope), "the whole ad account"),
            platform_name=platform.label,
            platform_guidance=platform.guidance,
            snapshot=json.dumps(snapshot, indent=1, default=str),
            comparison=json.dumps(comparison, indent=1, default=str) if comparison else "",
            previous_review=json.dumps(previous.result, indent=1) if previous else "",
        )
        request = LlmRequest(
            tier=prompt.tier,
            system=[SystemBlock(system)],
            messages=[Message.user("Answer with the structured review.")],
        )
        result, _ = complete_structured(
            get_llm_service(workspace.pk),
            request,
            ReviewResult,
            workspace_id=workspace.pk,
            prompt_key=f"{PROMPT_KEY}@v{prompt.version}",
        )
    except DomainError as exc:
        return _fail(review, exc.code.value)
    except Exception:
        log.exception("ad_review_crashed")
        return _fail(review, ErrorCode.UNKNOWN.value)
    review.snapshot = snapshot
    review.previous = previous
    review.comparison = comparison
    review.result = result.model_dump(mode="json")
    review.status = "done"
    review.thinking_since = None
    review.save()
    return "done"


def _fail(review: AdReview, code: str) -> str:
    review.status = "failed"
    review.error_code = code
    review.thinking_since = None
    review.save(update_fields=["status", "error_code", "thinking_since", "updated_at"])
    return "failed"
