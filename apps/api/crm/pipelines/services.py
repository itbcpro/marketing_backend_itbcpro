"""Pipeline and opportunity use cases (public surface of the module)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, QuerySet, Sum, Value
from django.utils import timezone

from audit.actors import Actor
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.contacts import services as contacts
from crm.events.publisher import publish
from crm.fields import services as fields
from crm.fields.models import ObjectType
from crm.pipelines import clean, events, filters, insights, rules, stats
from crm.pipelines.models import (
    DealPriority,
    LostReason,
    Opportunity,
    OpportunityStatus,
    Pipeline,
    Stage,
    StageChange,
)
from workspaces.models import Membership
from workspaces.scope import restrict

OPEN = OpportunityStatus.OPEN
PERCENT = 100
TAG_SCAN_LIMIT = 5_000


def _user_id(actor: Actor) -> uuid.UUID | None:
    return uuid.UUID(actor.id) if actor.type == "human" else None


# ---- pipelines and stages -------------------------------------------------------------


def list_pipelines(workspace_id: uuid.UUID, *, archived: bool = False) -> QuerySet[Pipeline]:
    rows = Pipeline.objects.for_workspace(workspace_id).prefetch_related("stages")
    return rows.filter(archived_at__isnull=not archived)


def get_pipeline(workspace_id: uuid.UUID, pipeline_id: uuid.UUID) -> Pipeline:
    found = Pipeline.objects.for_workspace(workspace_id).filter(pk=pipeline_id).first()
    if found is None:
        raise NotFoundError("pipeline not found")
    return found


def get_stage(workspace_id: uuid.UUID, stage_id: uuid.UUID) -> Stage:
    found = Stage.objects.for_workspace(workspace_id).filter(pk=stage_id).first()
    if found is None:
        raise NotFoundError("stage not found")
    return found


def create_pipeline(
    workspace_id: uuid.UUID, *, name: str, stages: list[tuple[str, int]]
) -> Pipeline:
    """A pipeline needs at least one stage; ``stages`` are (name, win probability) in order."""
    if not stages:
        raise ValidationFailedError("pipeline_needs_stage")
    try:
        with transaction.atomic():
            pipeline = Pipeline.objects.create(workspace_id=workspace_id, name=clean.name(name))
            for position, (stage_name, probability) in enumerate(stages):
                Stage.objects.create(
                    workspace_id=workspace_id,
                    pipeline=pipeline,
                    name=clean.name(stage_name),
                    position=position,
                    probability=clean.probability(probability),
                )
    except IntegrityError as exc:
        raise ConflictError("pipeline or stage name already exists") from exc
    return pipeline


def rename_pipeline(workspace_id: uuid.UUID, pipeline_id: uuid.UUID, name: str) -> Pipeline:
    pipeline = get_pipeline(workspace_id, pipeline_id)
    pipeline.name = clean.name(name)
    try:
        with transaction.atomic():
            pipeline.save(update_fields=["name", "updated_at"])
    except IntegrityError as exc:
        raise ConflictError("pipeline name already exists") from exc
    return pipeline


def archive_pipeline(workspace_id: uuid.UUID, pipeline_id: uuid.UUID) -> None:
    """Archiving hides a pipeline from the board; its deals are kept."""
    pipeline = get_pipeline(workspace_id, pipeline_id)
    pipeline.archived_at = timezone.now()
    pipeline.save(update_fields=["archived_at", "updated_at"])


def add_stage(
    workspace_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    *,
    name: str,
    probability: int = 0,
    rotting_days: int | None = None,
    required_fields: list[str] | None = None,
) -> Stage:
    pipeline = get_pipeline(workspace_id, pipeline_id)
    last = pipeline.stages.order_by("-position").values_list("position", flat=True).first()
    try:
        with transaction.atomic():
            return Stage.objects.create(
                workspace_id=workspace_id,
                pipeline=pipeline,
                name=clean.name(name),
                position=0 if last is None else last + 1,
                probability=clean.probability(probability),
                rotting_days=clean.rotting_days(rotting_days),
                required_fields=clean.required_fields(required_fields or []),
            )
    except IntegrityError as exc:
        raise ConflictError("stage name already exists") from exc


# Editable stage fields and how each is cleaned. A new field is a new entry here.
_STAGE_FIELDS: dict[str, Callable[[Any], Any]] = {
    "name": clean.name,
    "probability": clean.probability,
    "rotting_days": clean.rotting_days,
    "required_fields": clean.required_fields,
}
_STAGE_CLEARABLE = {"rotting_days"}


def update_stage(workspace_id: uuid.UUID, stage_id: uuid.UUID, **changes: Any) -> Stage:
    """Apply the given changes. ``None`` leaves a field alone, except where clearing is allowed."""
    stage = get_stage(workspace_id, stage_id)
    applied: list[str] = []
    for key, raw in changes.items():
        if key not in _STAGE_FIELDS:
            raise ValidationFailedError("unknown_field")
        if raw is None and key not in _STAGE_CLEARABLE:
            continue
        setattr(stage, key, _STAGE_FIELDS[key](raw))
        applied.append(key)
    try:
        with transaction.atomic():
            stage.save(update_fields=[*applied, "updated_at"])
    except IntegrityError as exc:
        raise ConflictError("stage name already exists") from exc
    return stage


def reorder_stages(
    workspace_id: uuid.UUID, pipeline_id: uuid.UUID, stage_ids: list[uuid.UUID]
) -> list[Stage]:
    """``stage_ids`` must list every stage of the pipeline exactly once."""
    pipeline = get_pipeline(workspace_id, pipeline_id)
    stages = {s.pk: s for s in pipeline.stages.all()}
    if set(stage_ids) != set(stages) or len(stage_ids) != len(stages):
        raise ValidationFailedError("stage_order_incomplete")
    with transaction.atomic():
        for position, stage_id in enumerate(stage_ids):
            Stage.objects.for_workspace(workspace_id).filter(pk=stage_id).update(position=position)
    return list(pipeline.stages.all())


def delete_stage(
    workspace_id: uuid.UUID, stage_id: uuid.UUID, *, move_to: uuid.UUID | None = None
) -> None:
    """Deals in the stage move to ``move_to`` (same pipeline); the last stage cannot go."""
    stage = get_stage(workspace_id, stage_id)
    siblings = Stage.objects.for_workspace(workspace_id).filter(pipeline_id=stage.pipeline_id)
    if siblings.count() <= 1:
        raise ConflictError("a pipeline needs at least one stage")
    with transaction.atomic():
        if stage.opportunities.exists():
            target = siblings.filter(pk=move_to).exclude(pk=stage.pk).first() if move_to else None
            if target is None:
                raise ConflictError("stage_has_deals")
            stage.opportunities.update(stage=target, last_stage_change_at=timezone.now())
        stage.delete()


# ---- lost reasons -------------------------------------------------------------------


def list_lost_reasons(workspace_id: uuid.UUID) -> QuerySet[LostReason]:
    return LostReason.objects.for_workspace(workspace_id).filter(archived_at__isnull=True)


def create_lost_reason(workspace_id: uuid.UUID, name: str) -> LostReason:
    try:
        with transaction.atomic():
            return LostReason.objects.create(workspace_id=workspace_id, name=clean.name(name))
    except IntegrityError as exc:
        raise ConflictError("lost reason already exists") from exc


def archive_lost_reason(workspace_id: uuid.UUID, reason_id: uuid.UUID) -> None:
    row = LostReason.objects.for_workspace(workspace_id).filter(pk=reason_id).first()
    if row is None:
        raise NotFoundError("lost reason not found")
    row.archived_at = timezone.now()
    row.save(update_fields=["archived_at", "updated_at"])


# ---- opportunities -------------------------------------------------------------------


def _scoped(workspace_id: uuid.UUID, membership: Membership | None) -> QuerySet[Opportunity]:
    rows = Opportunity.objects.for_workspace(workspace_id).select_related("stage", "contact")
    if membership is None:
        return rows
    return restrict(rows, membership)


def get_opportunity(
    workspace_id: uuid.UUID, opportunity_id: uuid.UUID, membership: Membership | None = None
) -> Opportunity:
    found = _scoped(workspace_id, membership).filter(pk=opportunity_id).first()
    if found is None:
        raise NotFoundError("opportunity not found")
    return found


def list_opportunities(
    workspace_id: uuid.UUID,
    membership: Membership | None = None,
    *,
    sort: str | None = None,
    **criteria: Any,
) -> QuerySet[Opportunity]:
    """Deals matching ``criteria`` (the keys of :data:`crm.pipelines.filters.FILTERS`)."""
    rows = filters.apply_filters(_scoped(workspace_id, membership), criteria)
    return filters.apply_sort(rows, sort)


@dataclass(frozen=True)
class DealSummary:
    count: int
    value_micros: int
    weighted_micros: int
    average_micros: int
    won: int
    lost: int


def deal_summary(
    workspace_id: uuid.UUID, membership: Membership | None = None, **criteria: Any
) -> DealSummary:
    """KPIs of the deals matching ``criteria``. Won and lost counts ignore the status filter, so
    the win rate stays meaningful while the board shows only open deals."""
    scoped = _scoped(workspace_id, membership)
    totals = stats.summarize(filters.apply_filters(scoped, criteria))
    any_status = filters.apply_filters(scoped, {k: v for k, v in criteria.items() if k != "status"})
    closed = stats.outcomes(any_status)
    count = totals["count"]
    return DealSummary(
        count=count,
        value_micros=totals["value_micros"],
        weighted_micros=totals["weighted_micros"],
        average_micros=totals["value_micros"] // count if count else 0,
        won=closed["won"],
        lost=closed["lost"],
    )


def pipeline_insights(
    workspace_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    membership: Membership | None = None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Forecast, funnel, time in stage, losses and owners of one pipeline over a period
    (the last ``CRM_INSIGHTS_DAYS`` days by default), for the deals the viewer may see."""
    pipeline = get_pipeline(workspace_id, pipeline_id)
    end = until or timezone.now()
    start = since or end - timedelta(days=settings.CRM_INSIGHTS_DAYS)
    deals = _scoped(workspace_id, membership).filter(pipeline=pipeline)
    cohort = deals.filter(created_at__gte=start, created_at__lt=end)
    return {
        "since": start,
        "until": end,
        "totals": insights.totals(deals, start, end),
        "forecast": insights.forecast_by_month(deals),
        "funnel": insights.funnel(workspace_id, pipeline, cohort),
        "time_in_stage": insights.time_in_stage(workspace_id, pipeline, cohort, end),
        "lost_reasons": insights.lost_reasons(deals, start, end),
        "owners": insights.owners(deals, start, end),
    }


def tags_in_use(workspace_id: uuid.UUID, membership: Membership | None = None) -> list[str]:
    """Every tag on any visible deal, sorted, for filter menus."""
    rows = _scoped(workspace_id, membership).exclude(tags=[]).values_list("tags", flat=True)
    return sorted({tag for tags in rows[:TAG_SCAN_LIMIT] for tag in tags})


def _member(workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    if not Membership.objects.filter(workspace_id=workspace_id, user_id=user_id).exists():
        raise ValidationFailedError("assignee_not_member")


def create_opportunity(
    workspace_id: uuid.UUID,
    actor: Actor,
    *,
    contact_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    name: str,
    stage_id: uuid.UUID | None = None,
    value_micros: int = 0,
    currency: str | None = None,
    assigned_to: uuid.UUID | None = None,
    source: str = "",
    expected_close_date: date | None = None,
    priority: str = DealPriority.NORMAL,
    tags: list[str] | None = None,
    probability_override: int | None = None,
    custom: dict[str, Any] | None = None,
) -> Opportunity:
    contact = contacts.get_contact(workspace_id, contact_id)
    pipeline = get_pipeline(workspace_id, pipeline_id)
    if pipeline.archived_at is not None:
        raise ConflictError("pipeline is archived")
    stage = (
        get_stage(workspace_id, stage_id)
        if stage_id
        else pipeline.stages.order_by("position").first()
    )
    if stage is None or stage.pipeline_id != pipeline.pk:
        raise ValidationFailedError("stage_not_in_pipeline")
    if assigned_to:
        _member(workspace_id, assigned_to)
    now = timezone.now()
    with transaction.atomic():
        deal = Opportunity(
            workspace_id=workspace_id,
            contact_id=contact.pk,
            pipeline=pipeline,
            stage=stage,
            name=clean.deal_name(name),
            value_micros=clean.value_micros(value_micros),
            currency=clean.currency(currency or settings.CRM_WALLET_CURRENCY),
            assigned_to_id=assigned_to,
            source=clean.source(source),
            expected_close_date=clean.close_date(expected_close_date),
            priority=clean.priority(priority),
            tags=clean.tags(tags or []),
            probability_override=clean.probability_override(probability_override),
            last_stage_change_at=now,
            last_status_change_at=now,
        )
        _require_entry(deal, stage)
        deal.save()
        if custom:
            fields.set_values(
                workspace_id=workspace_id,
                object_type=ObjectType.OPPORTUNITY,
                record_id=deal.pk,
                values=custom,
            )
        StageChange.objects.create(
            workspace_id=workspace_id,
            opportunity=deal,
            from_stage=None,
            to_stage=stage,
            changed_by_id=_user_id(actor),
        )
        publish(
            "crm.opportunity.created",
            workspace_id=workspace_id,
            actor=actor,
            data=events.OpportunityCreated(
                opportunity_id=deal.pk,
                contact_id=contact.pk,
                pipeline_id=pipeline.pk,
                stage_id=stage.pk,
                value_micros=deal.value_micros,
                currency=deal.currency,
            ),
        )
    return deal


def _require_entry(deal: Opportunity, stage: Stage) -> None:
    """Refuse a deal that lacks what ``stage`` demands (see :mod:`crm.pipelines.rules`)."""
    lacking = rules.missing(deal, stage)
    if lacking:
        raise ValidationFailedError(
            "stage_requirements_missing", errors=dict.fromkeys(lacking, "required_for_stage")
        )


# Editable deal fields and how each is cleaned. A new field is a new entry here.
_DEAL_FIELDS: dict[str, Callable[[Any], Any]] = {
    "name": clean.deal_name,
    "value_micros": clean.value_micros,
    "currency": clean.currency,
    "source": clean.source,
    "expected_close_date": clean.close_date,
    "priority": clean.priority,
    "tags": clean.tags,
    "probability_override": clean.probability_override,
}
_DEAL_CLEARABLE = {"expected_close_date", "probability_override"}


def update_opportunity(
    workspace_id: uuid.UUID,
    actor: Actor,
    deal: Opportunity,
    *,
    custom: dict[str, Any] | None = None,
    **changes: Any,
) -> Opportunity:
    """Apply the given changes. ``None`` leaves a field alone, except where clearing is allowed."""
    for key, raw in changes.items():
        if key not in _DEAL_FIELDS:
            raise ValidationFailedError("unknown_field")
        if raw is None and key not in _DEAL_CLEARABLE:
            continue
        setattr(deal, key, _DEAL_FIELDS[key](raw))
    with transaction.atomic():
        deal.save()
        if custom:
            fields.set_values(
                workspace_id=workspace_id,
                object_type=ObjectType.OPPORTUNITY,
                record_id=deal.pk,
                values=custom,
            )
    return deal


def assign_opportunity(
    workspace_id: uuid.UUID, actor: Actor, deal: Opportunity, user_id: uuid.UUID | None
) -> Opportunity:
    if user_id:
        _member(workspace_id, user_id)
    with transaction.atomic():
        deal.assigned_to_id = user_id
        deal.save(update_fields=["assigned_to", "updated_at"])
        publish(
            "crm.opportunity.assigned",
            workspace_id=workspace_id,
            actor=actor,
            data=events.OpportunityAssigned(
                opportunity_id=deal.pk, contact_id=deal.contact_id, assigned_to=user_id
            ),
        )
    return deal


def move_opportunity(
    workspace_id: uuid.UUID, actor: Actor, deal: Opportunity, stage_id: uuid.UUID
) -> Opportunity:
    """Move a deal to another stage of its own pipeline. A closed deal must be reopened first."""
    if deal.status != OPEN:
        raise ConflictError("opportunity is closed")
    stage = get_stage(workspace_id, stage_id)
    if stage.pipeline_id != deal.pipeline_id:
        raise ValidationFailedError("stage_not_in_pipeline")
    if stage.pk == deal.stage_id:
        return deal
    _require_entry(deal, stage)
    now = timezone.now()
    with transaction.atomic():
        previous = deal.stage_id
        deal.stage = stage
        deal.last_stage_change_at = now
        deal.save(update_fields=["stage", "last_stage_change_at", "updated_at"])
        StageChange.objects.create(
            workspace_id=workspace_id,
            opportunity=deal,
            from_stage_id=previous,
            to_stage=stage,
            changed_by_id=_user_id(actor),
        )
        publish(
            "crm.opportunity.stage_changed",
            workspace_id=workspace_id,
            actor=actor,
            data=events.OpportunityStageChanged(
                opportunity_id=deal.pk,
                contact_id=deal.contact_id,
                from_stage_id=previous,
                to_stage_id=stage.pk,
            ),
        )
    return deal


def _set_status(
    workspace_id: uuid.UUID,
    actor: Actor,
    deal: Opportunity,
    status: str,
    *,
    lost_reason_id: uuid.UUID | None = None,
) -> Opportunity:
    if deal.status == status:
        return deal
    now = timezone.now()
    with transaction.atomic():
        previous = deal.status
        deal.status = status
        deal.last_status_change_at = now
        deal.closed_at = None if status == OPEN else now
        deal.lost_reason_id = lost_reason_id
        deal.save(
            update_fields=[
                "status",
                "last_status_change_at",
                "closed_at",
                "lost_reason",
                "updated_at",
            ]
        )
        publish(
            "crm.opportunity.status_changed",
            workspace_id=workspace_id,
            actor=actor,
            data=events.OpportunityStatusChanged(
                opportunity_id=deal.pk,
                contact_id=deal.contact_id,
                from_status=previous,
                to_status=status,
                lost_reason_id=lost_reason_id,
            ),
        )
    return deal


def win(workspace_id: uuid.UUID, actor: Actor, deal: Opportunity) -> Opportunity:
    return _set_status(workspace_id, actor, deal, OpportunityStatus.WON)


def lose(
    workspace_id: uuid.UUID,
    actor: Actor,
    deal: Opportunity,
    *,
    lost_reason_id: uuid.UUID | None = None,
) -> Opportunity:
    if lost_reason_id and not list_lost_reasons(workspace_id).filter(pk=lost_reason_id).exists():
        raise ValidationFailedError("unknown_lost_reason")
    return _set_status(
        workspace_id, actor, deal, OpportunityStatus.LOST, lost_reason_id=lost_reason_id
    )


def abandon(workspace_id: uuid.UUID, actor: Actor, deal: Opportunity) -> Opportunity:
    return _set_status(workspace_id, actor, deal, OpportunityStatus.ABANDONED)


def reopen(workspace_id: uuid.UUID, actor: Actor, deal: Opportunity) -> Opportunity:
    return _set_status(workspace_id, actor, deal, OPEN)


def delete_opportunity(workspace_id: uuid.UUID, deal: Opportunity) -> None:
    with transaction.atomic():
        fields.delete_values_of(workspace_id, ObjectType.OPPORTUNITY, deal.pk)
        deal.delete()


def custom_values(
    workspace_id: uuid.UUID, deals: list[Opportunity]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Custom field values of several deals, by deal id."""
    return fields.get_values(workspace_id, ObjectType.OPPORTUNITY, [d.pk for d in deals])


def history(workspace_id: uuid.UUID, deal: Opportunity) -> list[StageChange]:
    rows = StageChange.objects.for_workspace(workspace_id).filter(opportunity=deal)
    return list(rows.select_related("from_stage", "to_stage"))


# ---- board, stale deals and forecast ------------------------------------------------


@dataclass(frozen=True)
class StageTotals:
    stage_id: uuid.UUID
    count: int
    value_micros: int
    weighted_micros: int


def stage_totals(
    workspace_id: uuid.UUID, pipeline_id: uuid.UUID, membership: Membership | None = None
) -> list[StageTotals]:
    """Open deals per stage with their value and probability-weighted value (per currency
    the numbers are summed as stored, so a pipeline is expected to use one currency). A deal's
    own probability, when set, replaces the probability of its stage."""
    pipeline = get_pipeline(workspace_id, pipeline_id)
    open_deals = _scoped(workspace_id, membership).filter(pipeline=pipeline, status=OPEN)
    out: list[StageTotals] = []
    for stage in pipeline.stages.all():
        row = open_deals.filter(stage=stage).aggregate(
            count=Count("id"),
            value=Sum("value_micros"),
            weighted=Sum(stats.weighted_value(Value(stage.probability))),
        )
        out.append(
            StageTotals(
                stage_id=stage.pk,
                count=int(row["count"] or 0),
                value_micros=int(row["value"] or 0),
                weighted_micros=int(row["weighted"] or 0) // PERCENT,
            )
        )
    return out


def stale_opportunities(
    workspace_id: uuid.UUID, membership: Membership | None = None, *, days: int | None = None
) -> QuerySet[Opportunity]:
    """Open deals that have sat in their stage longer than its threshold. A stage may set its own
    ``rotting_days``; the others use ``days`` or the workspace default."""
    default_days = days if days is not None else settings.CRM_STALE_DEAL_DAYS
    now = timezone.now()
    overrides = (
        Stage.objects.for_workspace(workspace_id)
        .filter(rotting_days__isnull=False)
        .values_list("rotting_days", flat=True)
        .distinct()
    )
    limit = Q(
        stage__rotting_days__isnull=True,
        last_stage_change_at__lt=now - timedelta(days=default_days),
    )
    for own in overrides:
        limit |= Q(
            stage__rotting_days=own, last_stage_change_at__lt=now - timedelta(days=int(own or 0))
        )
    return (
        _scoped(workspace_id, membership)
        .filter(status=OPEN)
        .filter(limit)
        .order_by("last_stage_change_at")
    )


def bulk_move(
    workspace_id: uuid.UUID,
    actor: Actor,
    deal_ids: list[uuid.UUID],
    stage_id: uuid.UUID,
    membership: Membership | None = None,
) -> int:
    """Move many deals at once; deals that cannot move (closed, other pipeline) are skipped."""
    if len(deal_ids) > settings.CRM_BULK_MAX_RECORDS:
        raise ValidationFailedError("too_many_records")
    moved = 0
    for deal in _scoped(workspace_id, membership).filter(pk__in=deal_ids):
        try:
            before = deal.stage_id
            move_opportunity(workspace_id, actor, deal, stage_id)
        except ConflictError, ValidationFailedError:
            continue
        moved += deal.stage_id != before
    return moved


def contact_deal_count(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> int:
    return Opportunity.objects.for_workspace(workspace_id).filter(contact_id=contact_id).count()


pipeline_report = stats.pipeline_report
won_by_owner = stats.won_by_owner
won_value_by_contact = stats.won_value_by_contact
