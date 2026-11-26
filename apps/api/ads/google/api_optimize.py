"""Google Ads campaign optimiser: start a run, read the plan, apply the items the owner chose."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from ads.google import optimize
from ads.models import AdEntity, AdLevel, AdOptimization
from core.errors import NotFoundError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["ads-google"], auth=api_auth)
RECENT = 5


class GoalIn(Schema):
    goal: str = ""


class ApplyOptimizationIn(Schema):
    keys: list[str]
    apply_now: bool = False


class OptimizationOut(Schema):
    id: uuid.UUID
    entity_id: uuid.UUID
    goal: str
    status: str
    error_code: str
    thinking_since: datetime | None
    landing: list[dict[str, Any]]
    plan: dict[str, Any] | None
    applied: dict[str, Any]
    created_at: datetime


def _out(row: AdOptimization) -> OptimizationOut:
    return OptimizationOut(
        id=row.pk,
        entity_id=row.entity_id,
        goal=row.goal,
        status=row.status,
        error_code=row.error_code,
        thinking_since=row.thinking_since,
        landing=list(row.landing),
        plan=row.plan,
        applied=optimize.item_states(row),
        created_at=row.created_at,
    )


def _campaign(request: HttpRequest, entity_id: uuid.UUID, permission: Permission) -> AdEntity:
    membership = resolve_workspace(request, permission)
    found = (
        AdEntity.objects.filter(
            pk=entity_id, workspace_id=membership.workspace_id, level=AdLevel.CAMPAIGN
        )
        .select_related("connection")
        .first()
    )
    if found is None:
        raise NotFoundError("campaign not found")
    return found


@router.get(
    "/optimize/campaigns/{entity_id}",
    response=list[OptimizationOut],
    operation_id="google_ads_optimize_list",
)
def list_runs(request: HttpRequest, entity_id: uuid.UUID) -> list[OptimizationOut]:
    campaign = _campaign(request, entity_id, Permission.VIEW)
    return [_out(r) for r in AdOptimization.objects.filter(entity=campaign)[:RECENT]]


@router.post(
    "/optimize/campaigns/{entity_id}",
    response=OptimizationOut,
    operation_id="google_ads_optimize_start",
)
def start_run(request: HttpRequest, entity_id: uuid.UUID, body: GoalIn) -> OptimizationOut:
    campaign = _campaign(request, entity_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    return _out(optimize.start(campaign.connection, user.pk, campaign, body.goal))


@router.get("/optimize/{run_id}", response=OptimizationOut, operation_id="google_ads_optimize_get")
def get_run(request: HttpRequest, run_id: uuid.UUID) -> OptimizationOut:
    membership = resolve_workspace(request, Permission.VIEW)
    return _out(optimize.get(membership.workspace_id, run_id))


@router.post(
    "/optimize/{run_id}/apply",
    response=OptimizationOut,
    operation_id="google_ads_optimize_apply",
)
def apply_run(
    request: HttpRequest, run_id: uuid.UUID, body: ApplyOptimizationIn
) -> OptimizationOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    row = optimize.get(membership.workspace_id, run_id)
    updated, _ = optimize.apply(row, user, body.keys, apply_now=body.apply_now)
    return _out(updated)
