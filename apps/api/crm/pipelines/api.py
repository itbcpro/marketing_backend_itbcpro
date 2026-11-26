"""Pipelines, stages, lost reasons and opportunities."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Schema, Status

from audit.actors import Actor
from crm.activity import services as activity
from crm.contacts import services as contacts
from crm.pipelines import bulk, rules, services
from crm.pipelines.models import LostReason, Opportunity, Pipeline, Stage, StageChange
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-pipelines"], auth=api_auth)


class StageIn(Schema):
    name: str
    probability: int = 0
    rotting_days: int | None = None
    required_fields: list[str] = []


class StagePatch(Schema):
    name: str | None = None
    probability: int | None = None
    rotting_days: int | None = None
    required_fields: list[str] | None = None


class StageOut(Schema):
    id: uuid.UUID
    name: str
    position: int
    probability: int
    rotting_days: int | None
    required_fields: list[str]


class PipelineIn(Schema):
    name: str
    stages: list[StageIn]


class PipelineOut(Schema):
    id: uuid.UUID
    name: str
    stages: list[StageOut]


class RenameIn(Schema):
    name: str


class StageOrderIn(Schema):
    stage_ids: list[uuid.UUID]


class LostReasonOut(Schema):
    id: uuid.UUID
    name: str


class OpportunityIn(Schema):
    contact_id: uuid.UUID
    pipeline_id: uuid.UUID
    name: str
    stage_id: uuid.UUID | None = None
    value_micros: int = 0
    currency: str | None = None
    assigned_to: uuid.UUID | None = None
    source: str = ""
    expected_close_date: date | None = None
    priority: str = "normal"
    tags: list[str] = []
    probability_override: int | None = None
    custom: dict[str, Any] | None = None


class OpportunityPatch(Schema):
    name: str | None = None
    value_micros: int | None = None
    currency: str | None = None
    source: str | None = None
    expected_close_date: date | None = None
    priority: str | None = None
    tags: list[str] | None = None
    probability_override: int | None = None
    custom: dict[str, Any] | None = None


class OpportunityOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str
    pipeline_id: uuid.UUID
    stage_id: uuid.UUID
    name: str
    status: str
    value_micros: int
    currency: str
    assigned_to: uuid.UUID | None
    source: str
    expected_close_date: date | None
    priority: str
    tags: list[str]
    probability_override: int | None
    lost_reason_id: uuid.UUID | None
    last_stage_change_at: datetime
    closed_at: datetime | None
    created_at: datetime
    custom: dict[str, Any]
    open_tasks: int
    overdue_tasks: int
    next_task_due_at: datetime | None


class DealFilters(Schema):
    """Every way to narrow the deal list; each maps to an entry of ``filters.FILTERS``."""

    pipeline_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
    status: str | None = None
    contact_id: uuid.UUID | None = None
    assigned_to: uuid.UUID | None = None
    unassigned: bool | None = None
    priority: str | None = None
    source: str | None = None
    tag: str | None = None
    q: str | None = None
    min_value_micros: int | None = None
    max_value_micros: int | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    close_after: date | None = None
    close_before: date | None = None


class SummaryOut(Schema):
    count: int
    value_micros: int
    weighted_micros: int
    average_micros: int
    won: int
    lost: int


class MoveIn(Schema):
    stage_id: uuid.UUID


class DealAssignIn(Schema):
    user_id: uuid.UUID | None = None


class LoseIn(Schema):
    lost_reason_id: uuid.UUID | None = None


class BulkMoveIn(Schema):
    deal_ids: list[uuid.UUID]
    stage_id: uuid.UUID


class BulkMoveOut(Schema):
    moved: int


class StageChangeOut(Schema):
    from_stage_id: uuid.UUID | None
    to_stage_id: uuid.UUID | None
    changed_by: uuid.UUID | None
    at: datetime


class TotalsOut(Schema):
    stage_id: uuid.UUID
    count: int
    value_micros: int
    weighted_micros: int


def _stage(row: Stage) -> StageOut:
    return StageOut(
        id=row.pk,
        name=row.name,
        position=row.position,
        probability=row.probability,
        rotting_days=row.rotting_days,
        required_fields=list(row.required_fields),
    )


def _pipeline(row: Pipeline) -> PipelineOut:
    return PipelineOut(id=row.pk, name=row.name, stages=[_stage(s) for s in row.stages.all()])


def _deal(
    row: Opportunity,
    custom: dict[str, Any] | None = None,
    tasks: dict[str, Any] | None = None,
) -> OpportunityOut:
    return OpportunityOut(
        id=row.pk,
        contact_id=row.contact_id,
        contact_name=str(row.contact),
        pipeline_id=row.pipeline_id,
        stage_id=row.stage_id,
        name=row.name,
        status=row.status,
        value_micros=row.value_micros,
        currency=row.currency,
        assigned_to=row.assigned_to_id,
        source=row.source,
        expected_close_date=row.expected_close_date,
        priority=row.priority,
        tags=list(row.tags),
        probability_override=row.probability_override,
        lost_reason_id=row.lost_reason_id,
        last_stage_change_at=row.last_stage_change_at,
        closed_at=row.closed_at,
        created_at=row.created_at,
        custom=custom or {},
        open_tasks=(tasks or {}).get("open", 0),
        overdue_tasks=(tasks or {}).get("overdue", 0),
        next_task_due_at=(tasks or {}).get("next_due_at"),
    )


def _deals(workspace_id: uuid.UUID, rows: list[Opportunity]) -> list[OpportunityOut]:
    values = services.custom_values(workspace_id, rows)
    tasks = activity.open_task_summary(workspace_id, [r.pk for r in rows])
    return [_deal(r, values.get(r.pk), tasks.get(r.pk)) for r in rows]


def _one(workspace_id: uuid.UUID, row: Opportunity) -> OpportunityOut:
    return _deals(workspace_id, [row])[0]


def _change(row: StageChange) -> StageChangeOut:
    return StageChangeOut(
        from_stage_id=row.from_stage_id,
        to_stage_id=row.to_stage_id,
        changed_by=row.changed_by_id,
        at=row.created_at,
    )


# ---- pipelines ------------------------------------------------------------------------


@router.get("/pipelines", response=list[PipelineOut], operation_id="crm_pipelines_list")
def list_pipelines(request: HttpRequest) -> list[PipelineOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return [_pipeline(p) for p in services.list_pipelines(membership.workspace_id)]


@router.post("/pipelines", response={201: PipelineOut}, operation_id="crm_pipelines_create")
def create_pipeline(request: HttpRequest, payload: PipelineIn) -> Status[PipelineOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_PIPELINES)
    row = services.create_pipeline(
        membership.workspace_id,
        name=payload.name,
        stages=[(s.name, s.probability) for s in payload.stages],
    )
    return Status(201, _pipeline(row))


@router.patch("/pipelines/{pipeline_id}", response=PipelineOut, operation_id="crm_pipelines_rename")
def rename_pipeline(request: HttpRequest, pipeline_id: uuid.UUID, payload: RenameIn) -> PipelineOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_PIPELINES)
    return _pipeline(services.rename_pipeline(membership.workspace_id, pipeline_id, payload.name))


@router.delete(
    "/pipelines/{pipeline_id}", response={204: None}, operation_id="crm_pipelines_archive"
)
def archive_pipeline(request: HttpRequest, pipeline_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_PIPELINES)
    services.archive_pipeline(membership.workspace_id, pipeline_id)
    return Status(204, None)


@router.get(
    "/pipelines/{pipeline_id}/totals",
    response=list[TotalsOut],
    operation_id="crm_pipelines_totals",
)
def pipeline_totals(request: HttpRequest, pipeline_id: uuid.UUID) -> list[TotalsOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = services.stage_totals(membership.workspace_id, pipeline_id, membership)
    return [
        TotalsOut(
            stage_id=r.stage_id,
            count=r.count,
            value_micros=r.value_micros,
            weighted_micros=r.weighted_micros,
        )
        for r in rows
    ]


@router.post(
    "/pipelines/{pipeline_id}/stages", response={201: StageOut}, operation_id="crm_stages_create"
)
def add_stage(request: HttpRequest, pipeline_id: uuid.UUID, payload: StageIn) -> Status[StageOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_PIPELINES)
    row = services.add_stage(membership.workspace_id, pipeline_id, **payload.model_dump())
    return Status(201, _stage(row))


@router.put(
    "/pipelines/{pipeline_id}/stages/order",
    response=list[StageOut],
    operation_id="crm_stages_reorder",
)
def reorder_stages(
    request: HttpRequest, pipeline_id: uuid.UUID, payload: StageOrderIn
) -> list[StageOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_PIPELINES)
    rows = services.reorder_stages(membership.workspace_id, pipeline_id, payload.stage_ids)
    return [_stage(s) for s in rows]


@router.get("/stage-requirements", response=list[str], operation_id="crm_stage_requirements")
def stage_requirements(request: HttpRequest) -> list[str]:
    """What a stage may demand of a deal before it can enter (see ``crm.pipelines.rules``)."""
    resolve_workspace(request, Permission.CRM_VIEW)
    return rules.available()


@router.patch("/stages/{stage_id}", response=StageOut, operation_id="crm_stages_update")
def update_stage(request: HttpRequest, stage_id: uuid.UUID, payload: StagePatch) -> StageOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_PIPELINES)
    row = services.update_stage(
        membership.workspace_id, stage_id, **payload.model_dump(exclude_unset=True)
    )
    return _stage(row)


@router.delete("/stages/{stage_id}", response={204: None}, operation_id="crm_stages_delete")
def delete_stage(
    request: HttpRequest, stage_id: uuid.UUID, move_to: uuid.UUID | None = None
) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_PIPELINES)
    services.delete_stage(membership.workspace_id, stage_id, move_to=move_to)
    return Status(204, None)


# ---- lost reasons ---------------------------------------------------------------------


def _reason(row: LostReason) -> LostReasonOut:
    return LostReasonOut(id=row.pk, name=row.name)


@router.get("/lost-reasons", response=list[LostReasonOut], operation_id="crm_lost_reasons_list")
def list_lost_reasons(request: HttpRequest) -> list[LostReasonOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return [_reason(r) for r in services.list_lost_reasons(membership.workspace_id)]


@router.post("/lost-reasons", response={201: LostReasonOut}, operation_id="crm_lost_reasons_create")
def create_lost_reason(request: HttpRequest, payload: RenameIn) -> Status[LostReasonOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_PIPELINES)
    return Status(201, _reason(services.create_lost_reason(membership.workspace_id, payload.name)))


@router.delete(
    "/lost-reasons/{reason_id}", response={204: None}, operation_id="crm_lost_reasons_archive"
)
def archive_lost_reason(request: HttpRequest, reason_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_PIPELINES)
    services.archive_lost_reason(membership.workspace_id, reason_id)
    return Status(204, None)


# ---- opportunities --------------------------------------------------------------------


@router.get("/opportunities", response=list[OpportunityOut], operation_id="crm_opportunities_list")
def list_opportunities(
    request: HttpRequest,
    filters: Query[DealFilters],
    sort: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[OpportunityOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = services.list_opportunities(
        membership.workspace_id, membership, sort=sort, **filters.model_dump()
    )
    start = max(0, offset)
    return _deals(membership.workspace_id, list(rows[start : start + max(1, min(limit, 500))]))


@router.get("/opportunities/summary", response=SummaryOut, operation_id="crm_opportunities_summary")
def opportunities_summary(request: HttpRequest, filters: Query[DealFilters]) -> SummaryOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    found = services.deal_summary(membership.workspace_id, membership, **filters.model_dump())
    return SummaryOut(**asdict(found))


@router.get("/opportunities/tags", response=list[str], operation_id="crm_opportunities_tags")
def opportunity_tags(request: HttpRequest) -> list[str]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return services.tags_in_use(membership.workspace_id, membership)


@router.get(
    "/opportunities/stale", response=list[OpportunityOut], operation_id="crm_opportunities_stale"
)
def stale(request: HttpRequest) -> list[OpportunityOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = services.stale_opportunities(membership.workspace_id, membership)
    return _deals(membership.workspace_id, list(rows[:500]))


@router.post(
    "/opportunities", response={201: OpportunityOut}, operation_id="crm_opportunities_create"
)
def create_opportunity(request: HttpRequest, payload: OpportunityIn) -> Status[OpportunityOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    contacts.get_visible_contact(membership, payload.contact_id)
    row = services.create_opportunity(
        membership.workspace_id, Actor.human(membership.user_id), **payload.model_dump()
    )
    return Status(201, _one(membership.workspace_id, row))


@router.post(
    "/opportunities/bulk-move", response=BulkMoveOut, operation_id="crm_opportunities_bulk_move"
)
def bulk_move(request: HttpRequest, payload: BulkMoveIn) -> BulkMoveOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    moved = services.bulk_move(
        membership.workspace_id,
        Actor.human(membership.user_id),
        payload.deal_ids,
        payload.stage_id,
        membership,
    )
    return BulkMoveOut(moved=moved)


class BulkActionIn(Schema):
    deal_ids: list[uuid.UUID]
    action: str
    stage_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    lost_reason_id: uuid.UUID | None = None
    priority: str | None = None
    tag: str | None = None


class BulkActionOut(Schema):
    done: int
    skipped: int


@router.post("/opportunities/bulk", response=BulkActionOut, operation_id="crm_opportunities_bulk")
def bulk_action(request: HttpRequest, payload: BulkActionIn) -> BulkActionOut:
    """Apply one action to many deals; the permission follows the action (delete needs more)."""
    membership = resolve_workspace(request, bulk.action_named(payload.action).permission)
    result = bulk.apply(
        membership.workspace_id,
        Actor.human(membership.user_id),
        payload.deal_ids,
        payload.action,
        bulk.Params(
            stage_id=payload.stage_id,
            user_id=payload.user_id,
            lost_reason_id=payload.lost_reason_id,
            priority=payload.priority,
            tag=payload.tag,
        ),
        membership,
    )
    return BulkActionOut(done=result.done, skipped=result.skipped)


@router.get(
    "/opportunities/{deal_id}", response=OpportunityOut, operation_id="crm_opportunities_get"
)
def get_opportunity(request: HttpRequest, deal_id: uuid.UUID) -> OpportunityOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return _one(
        membership.workspace_id,
        services.get_opportunity(membership.workspace_id, deal_id, membership),
    )


@router.patch(
    "/opportunities/{deal_id}", response=OpportunityOut, operation_id="crm_opportunities_update"
)
def update_opportunity(
    request: HttpRequest, deal_id: uuid.UUID, payload: OpportunityPatch
) -> OpportunityOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    deal = services.get_opportunity(membership.workspace_id, deal_id, membership)
    row = services.update_opportunity(
        membership.workspace_id,
        Actor.human(membership.user_id),
        deal,
        **payload.model_dump(exclude_unset=True),
    )
    return _one(membership.workspace_id, row)


@router.delete(
    "/opportunities/{deal_id}", response={204: None}, operation_id="crm_opportunities_delete"
)
def delete_opportunity(request: HttpRequest, deal_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_DELETE)
    deal = services.get_opportunity(membership.workspace_id, deal_id, membership)
    services.delete_opportunity(membership.workspace_id, deal)
    return Status(204, None)


@router.get(
    "/opportunities/{deal_id}/history",
    response=list[StageChangeOut],
    operation_id="crm_opportunities_history",
)
def deal_history(request: HttpRequest, deal_id: uuid.UUID) -> list[StageChangeOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    deal = services.get_opportunity(membership.workspace_id, deal_id, membership)
    return [_change(c) for c in services.history(membership.workspace_id, deal)]


def _act(request: HttpRequest, deal_id: uuid.UUID) -> tuple[uuid.UUID, Actor, Opportunity]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    deal = services.get_opportunity(membership.workspace_id, deal_id, membership)
    return membership.workspace_id, Actor.human(membership.user_id), deal


@router.post(
    "/opportunities/{deal_id}/move", response=OpportunityOut, operation_id="crm_opportunities_move"
)
def move(request: HttpRequest, deal_id: uuid.UUID, payload: MoveIn) -> OpportunityOut:
    workspace_id, actor, deal = _act(request, deal_id)
    return _one(
        workspace_id, services.move_opportunity(workspace_id, actor, deal, payload.stage_id)
    )


@router.post(
    "/opportunities/{deal_id}/assign",
    response=OpportunityOut,
    operation_id="crm_opportunities_assign",
)
def assign(request: HttpRequest, deal_id: uuid.UUID, payload: DealAssignIn) -> OpportunityOut:
    workspace_id, actor, deal = _act(request, deal_id)
    return _one(
        workspace_id, services.assign_opportunity(workspace_id, actor, deal, payload.user_id)
    )


@router.post(
    "/opportunities/{deal_id}/win", response=OpportunityOut, operation_id="crm_opportunities_win"
)
def win(request: HttpRequest, deal_id: uuid.UUID) -> OpportunityOut:
    workspace_id, actor, deal = _act(request, deal_id)
    return _one(workspace_id, services.win(workspace_id, actor, deal))


@router.post(
    "/opportunities/{deal_id}/lose", response=OpportunityOut, operation_id="crm_opportunities_lose"
)
def lose(request: HttpRequest, deal_id: uuid.UUID, payload: LoseIn) -> OpportunityOut:
    workspace_id, actor, deal = _act(request, deal_id)
    return _one(
        workspace_id,
        services.lose(workspace_id, actor, deal, lost_reason_id=payload.lost_reason_id),
    )


@router.post(
    "/opportunities/{deal_id}/abandon",
    response=OpportunityOut,
    operation_id="crm_opportunities_abandon",
)
def abandon(request: HttpRequest, deal_id: uuid.UUID) -> OpportunityOut:
    workspace_id, actor, deal = _act(request, deal_id)
    return _one(workspace_id, services.abandon(workspace_id, actor, deal))


@router.post(
    "/opportunities/{deal_id}/reopen",
    response=OpportunityOut,
    operation_id="crm_opportunities_reopen",
)
def reopen(request: HttpRequest, deal_id: uuid.UUID) -> OpportunityOut:
    workspace_id, actor, deal = _act(request, deal_id)
    return _one(workspace_id, services.reopen(workspace_id, actor, deal))
