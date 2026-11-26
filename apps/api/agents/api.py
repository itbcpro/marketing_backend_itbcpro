"""Agents, runs and the command box."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Schema

from agents import command, runtime
from agents.models import AgentDefinition, AgentRun, CommandSession
from core.api.filtering import FilterSpec, apply_filters
from core.api.pagination import DEFAULT_LIMIT, Page, paginate
from core.errors import NotFoundError
from identity.authentication import api_auth
from llm.service import get_llm_service
from workspaces.deps import resolve_workspace
from workspaces.models import Workspace
from workspaces.permissions import Permission

router = Router(tags=["agents"], auth=api_auth)
RUN_FILTERS = {"status": FilterSpec("status"), "agent": FilterSpec("agent__name")}


class AgentOut(Schema):
    id: uuid.UUID
    name: str
    title: str
    tier: str
    tools: list[str]
    enabled: bool
    max_steps: int
    max_cost_micros: int


class RunOut(Schema):
    id: uuid.UUID
    agent: str
    goal: str
    status: str
    tainted: bool
    steps_count: int
    cost_micros: int
    input_tokens: int
    output_tokens: int
    error: str
    result: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None
    parent_id: uuid.UUID | None


def _run_out(run: AgentRun) -> RunOut:
    return RunOut(
        id=run.pk,
        agent=run.agent.name,
        goal=run.goal,
        status=run.status,
        tainted=run.tainted,
        steps_count=run.steps_count,
        cost_micros=run.cost_micros,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        error=run.error,
        result=run.result,
        created_at=run.created_at,
        finished_at=run.finished_at,
        parent_id=run.parent_id,
    )


class StepOut(Schema):
    index: int
    kind: str
    payload: dict[str, Any]
    cost_micros: int
    latency_ms: int


class RunFilters(Schema):
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    status: str | None = None
    agent: str | None = None


class CommandIn(Schema):
    text: str


class AnswersIn(Schema):
    answers: dict[str, str]


class CommandOut(Schema):
    id: uuid.UUID
    text: str
    status: str
    brief: dict[str, Any]
    answers: list[str]
    plan: dict[str, Any] | None
    run_id: uuid.UUID | None


def _command_out(session: CommandSession) -> CommandOut:
    plan = None
    if session.status in {"ready", "running"}:
        brief = command.Brief.model_validate(session.brief)
        plan = command.build_plan(
            brief, Workspace.objects.get(pk=session.workspace_id)
        ).model_dump()
    return CommandOut(
        id=session.pk,
        text=session.text,
        status=session.status,
        brief=session.brief,
        answers=session.answers,
        plan=plan,
        run_id=session.run_id,
    )


def _session(request: HttpRequest, session_id: uuid.UUID, permission: Permission) -> CommandSession:
    membership = resolve_workspace(request, permission)
    session = (
        CommandSession.objects.select_related("workspace")
        .filter(pk=session_id, workspace_id=membership.workspace_id)
        .first()
    )
    if session is None:
        raise NotFoundError("command not found")
    return session


@router.get("/", response=list[AgentOut], operation_id="agents_list")
def list_agents(request: HttpRequest) -> Any:
    membership = resolve_workspace(request, Permission.VIEW)
    return list(
        AgentDefinition.objects.filter(workspace_id=membership.workspace_id).order_by("name")
    )


@router.get("/runs", response=Page[RunOut], operation_id="runs_list")
def list_runs(request: HttpRequest, filters: Query[RunFilters]) -> Page[RunOut]:
    membership = resolve_workspace(request, Permission.VIEW)
    params = {k: v for k in RUN_FILTERS if (v := getattr(filters, k)) is not None}
    queryset = apply_filters(
        AgentRun.objects.filter(workspace_id=membership.workspace_id).select_related("agent"),
        params,
        RUN_FILTERS,
    )
    rows, next_cursor = paginate(queryset, cursor=filters.cursor, limit=filters.limit)
    return Page[RunOut](
        items=[_run_out(r) for r in rows],
        next_cursor=next_cursor,
    )


def _run(request: HttpRequest, run_id: uuid.UUID, permission: Permission) -> AgentRun:
    membership = resolve_workspace(request, permission)
    run = (
        AgentRun.objects.select_related("agent")
        .filter(pk=run_id, workspace_id=membership.workspace_id)
        .first()
    )
    if run is None:
        raise NotFoundError("run not found")
    return run


@router.get("/runs/{run_id}", response=RunOut, operation_id="runs_get")
def get_run(request: HttpRequest, run_id: uuid.UUID) -> RunOut:
    return _run_out(_run(request, run_id, Permission.VIEW))


@router.get("/runs/{run_id}/steps", response=list[StepOut], operation_id="runs_steps")
def run_steps(request: HttpRequest, run_id: uuid.UUID) -> Any:
    return list(_run(request, run_id, Permission.VIEW).steps.order_by("index"))


@router.post("/runs/{run_id}/cancel", response=RunOut, operation_id="runs_cancel")
def cancel(request: HttpRequest, run_id: uuid.UUID) -> RunOut:
    run = _run(request, run_id, Permission.EDIT_CONTENT)
    runtime.cancel_run(run)
    run.refresh_from_db()
    return _run_out(run)


class UsageRow(Schema):
    day: str
    tier: str
    calls: int
    cost_micros: int
    input_tokens: int
    output_tokens: int


@router.get("/usage", response=list[UsageRow], operation_id="agents_usage")
def usage(request: HttpRequest, days: int = 14) -> list[UsageRow]:
    """AI spend per day and model tier for the workspace."""
    from datetime import timedelta

    from django.db.models import Count, Sum
    from django.db.models.functions import TruncDate
    from django.utils import timezone

    from llm.models import LlmCall

    membership = resolve_workspace(request, Permission.VIEW)
    since = timezone.now() - timedelta(days=max(1, min(days, 90)))
    rows = (
        LlmCall.objects.filter(workspace_id=membership.workspace_id, created_at__gte=since)
        .annotate(day=TruncDate("created_at"))
        .values("day", "tier")
        .annotate(
            calls=Count("id"),
            cost=Sum("cost_micros"),
            tokens_in=Sum("input_tokens"),
            tokens_out=Sum("output_tokens"),
        )
        .order_by("-day", "tier")
    )
    return [
        UsageRow(
            day=r["day"].isoformat(),
            tier=r["tier"],
            calls=r["calls"],
            cost_micros=int(r["cost"] or 0),
            input_tokens=int(r["tokens_in"] or 0),
            output_tokens=int(r["tokens_out"] or 0),
        )
        for r in rows
    ]


@router.post("/commands", response=CommandOut, operation_id="commands_submit")
def submit_command(request: HttpRequest, payload: CommandIn) -> CommandOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    workspace = Workspace.objects.get(pk=membership.workspace_id)
    session = command.submit(
        workspace, membership.user, payload.text, get_llm_service(workspace.pk)
    )
    return _command_out(session)


@router.get("/commands/{session_id}", response=CommandOut, operation_id="commands_get")
def get_command(request: HttpRequest, session_id: uuid.UUID) -> CommandOut:
    return _command_out(_session(request, session_id, Permission.VIEW))


@router.post("/commands/{session_id}/answers", response=CommandOut, operation_id="commands_answer")
def answer_command(request: HttpRequest, session_id: uuid.UUID, payload: AnswersIn) -> CommandOut:
    session = _session(request, session_id, Permission.EDIT_CONTENT)
    return _command_out(
        command.answer(session, payload.answers, get_llm_service(session.workspace_id))
    )


@router.post("/commands/{session_id}/confirm", response=CommandOut, operation_id="commands_confirm")
def confirm_command(request: HttpRequest, session_id: uuid.UUID) -> CommandOut:
    session = _session(request, session_id, Permission.EDIT_CONTENT)
    command.confirm(session, request.user)  # type: ignore[arg-type]
    session.refresh_from_db()
    return _command_out(session)
