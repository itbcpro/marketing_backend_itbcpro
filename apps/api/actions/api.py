"""Approvals inbox, action detail, decisions, autonomy, guardrails and the kill switch."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Schema

from actions import services
from actions.models import ActionStatus, AutonomyGrant, Guardrail, ProposedAction
from audit.actors import Actor
from audit.services import record_event
from core.api.filtering import FilterSpec, apply_filters
from core.api.pagination import DEFAULT_LIMIT, Page, paginate
from core.errors import ValidationFailedError
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.models import Workspace
from workspaces.permissions import Permission

router = Router(tags=["actions"], auth=api_auth)

FILTERS = {
    "status": FilterSpec("status"),
    "kind": FilterSpec("kind"),
    "risk": FilterSpec("risk"),
    "requested_by": FilterSpec("requested_by"),
}
GRANT_LEVELS = range(1, 5)


class ActionOut(Schema):
    id: uuid.UUID
    kind: str
    summary: str
    status: str
    risk: str
    requested_by: str
    tainted: bool
    auto_approved: bool
    spend_micros: int
    payload: dict[str, Any]
    edited_payload: dict[str, Any] | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    violations: list[dict[str, str]]
    decision_reason: str
    decision_note: str
    error: str
    result: dict[str, Any] | None
    created_at: datetime
    expires_at: datetime | None
    run_id: uuid.UUID | None


class ActionFilters(Schema):
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    status: str | None = None
    kind: str | None = None
    risk: str | None = None
    requested_by: str | None = None


class ApproveIn(Schema):
    note: str = ""
    edits: dict[str, Any] | None = None


class RejectIn(Schema):
    reason: str
    note: str = ""


class KillSwitchIn(Schema):
    enabled: bool


class GrantIn(Schema):
    agent: str
    action_kind: str
    level: int
    limits: dict[str, Any] = {}


class GrantOut(Schema):
    id: uuid.UUID
    agent: str
    action_kind: str
    level: int
    limits: dict[str, Any]


class GuardrailIn(Schema):
    key: str
    config: dict[str, Any]
    enabled: bool = True


class GuardrailOut(Schema):
    key: str
    config: dict[str, Any]
    enabled: bool


class InboxOut(Schema):
    awaiting: int
    blocked: int
    kill_switch: bool


@router.get("/", response=Page[ActionOut], operation_id="actions_list")
def list_actions(request: HttpRequest, filters: Query[ActionFilters]) -> Page[ActionOut]:
    membership = resolve_workspace(request, Permission.VIEW)
    params = {k: v for k in FILTERS if (v := getattr(filters, k)) is not None}
    queryset = apply_filters(
        ProposedAction.objects.filter(workspace_id=membership.workspace_id), params, FILTERS
    )
    rows, next_cursor = paginate(queryset, cursor=filters.cursor, limit=filters.limit)
    return Page[ActionOut](
        items=[ActionOut.model_validate(r, from_attributes=True) for r in rows],
        next_cursor=next_cursor,
    )


@router.get("/inbox", response=InboxOut, operation_id="actions_inbox")
def inbox(request: HttpRequest) -> InboxOut:
    membership = resolve_workspace(request, Permission.VIEW)
    scoped = ProposedAction.objects.filter(workspace_id=membership.workspace_id)
    workspace = Workspace.objects.get(pk=membership.workspace_id)
    return InboxOut(
        awaiting=scoped.filter(status=ActionStatus.AWAITING_APPROVAL).count(),
        blocked=scoped.filter(status=ActionStatus.POLICY_BLOCKED).count(),
        kill_switch=workspace.writes_paused,
    )


@router.post("/kill-switch", response=InboxOut, operation_id="actions_kill_switch")
def kill_switch(request: HttpRequest, payload: KillSwitchIn) -> InboxOut:
    membership = resolve_workspace(request, Permission.USE_KILL_SWITCH)
    services.set_kill_switch(
        workspace_id=membership.workspace_id, enabled=payload.enabled, user=membership.user
    )
    return inbox(request)


@router.get("/{action_id}", response=ActionOut, operation_id="actions_get")
def get_action(request: HttpRequest, action_id: uuid.UUID) -> Any:
    membership = resolve_workspace(request, Permission.VIEW)
    return services.get_action(membership.workspace_id, action_id)


@router.post("/{action_id}/approve", response=ActionOut, operation_id="actions_approve")
def approve(request: HttpRequest, action_id: uuid.UUID, payload: ApproveIn) -> Any:
    membership = resolve_workspace(request, Permission.VIEW)
    return services.approve(
        workspace_id=membership.workspace_id,
        action_id=action_id,
        user=membership.user,
        note=payload.note,
        edits=payload.edits,
    )


@router.post("/{action_id}/reject", response=ActionOut, operation_id="actions_reject")
def reject(request: HttpRequest, action_id: uuid.UUID, payload: RejectIn) -> Any:
    membership = resolve_workspace(request, Permission.VIEW)
    return services.reject(
        workspace_id=membership.workspace_id,
        action_id=action_id,
        user=membership.user,
        reason=payload.reason,
        note=payload.note,
    )


@router.post("/{action_id}/rollback", response=ActionOut, operation_id="actions_rollback")
def rollback(request: HttpRequest, action_id: uuid.UUID) -> Any:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    return services.request_rollback(
        workspace_id=membership.workspace_id, action_id=action_id, user=membership.user
    )


@router.get("/autonomy/grants", response=list[GrantOut], operation_id="autonomy_list")
def list_grants(request: HttpRequest) -> Any:
    membership = resolve_workspace(request, Permission.VIEW)
    return list(AutonomyGrant.objects.filter(workspace_id=membership.workspace_id))


@router.put("/autonomy/grants", response=GrantOut, operation_id="autonomy_set")
def set_grant(request: HttpRequest, payload: GrantIn) -> Any:
    membership = resolve_workspace(request, Permission.CHANGE_AUTONOMY)
    if payload.level not in GRANT_LEVELS:
        raise ValidationFailedError("level must be between 1 and 4")
    grant, _ = AutonomyGrant.objects.update_or_create(
        workspace_id=membership.workspace_id,
        agent=payload.agent,
        action_kind=payload.action_kind,
        defaults={"level": payload.level, "limits": payload.limits},
    )
    record_event(
        action="autonomy.changed",
        actor=Actor.human(membership.user_id),
        workspace_id=membership.workspace_id,
        target_type="autonomy_grant",
        target_id=grant.pk,
        after={"agent": payload.agent, "kind": payload.action_kind, "level": payload.level},
    )
    return grant


@router.get("/guardrails/all", response=list[GuardrailOut], operation_id="guardrails_list")
def list_guardrails(request: HttpRequest) -> Any:
    membership = resolve_workspace(request, Permission.VIEW)
    return list(Guardrail.objects.filter(workspace_id=membership.workspace_id).order_by("key"))


@router.put("/guardrails/all", response=GuardrailOut, operation_id="guardrails_set")
def set_guardrail(request: HttpRequest, payload: GuardrailIn) -> Any:
    membership = resolve_workspace(request, Permission.MANAGE_GUARDRAILS)
    guardrail, _ = Guardrail.objects.update_or_create(
        workspace_id=membership.workspace_id,
        key=payload.key,
        defaults={"config": payload.config, "enabled": payload.enabled},
    )
    record_event(
        action="guardrail.changed",
        actor=Actor.human(membership.user_id),
        workspace_id=membership.workspace_id,
        target_type="guardrail",
        target_id=guardrail.pk,
        after={"key": payload.key, "enabled": payload.enabled},
    )
    return guardrail
