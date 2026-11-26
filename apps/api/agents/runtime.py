"""Agent run engine: the loop that alternates model calls and tool execution.

Every step is persisted before the next begins, so a run can be inspected, resumed after a worker
dies, and cancelled between steps. Cost, step and wall-clock budgets end the run cleanly.
"""

import json
import time
import uuid
from datetime import date

from django.db import transaction
from django.utils import timezone

from actions.models import ProposedAction
from agents.defaults import ensure_default_agents
from agents.models import TERMINAL_STATUSES, AgentDefinition, AgentRun, AgentStep, RunStatus
from agents.tools import ToolContext, ToolRegistry, execute_tool
from agents.tools import registry as default_registry
from core.errors import DomainError, NotFoundError
from core.logging import get_logger
from llm import prompts
from llm.ports import LlmRequest, LlmResponse, Message, SystemBlock, ToolResultBlock, ToolUseBlock
from llm.pricing import cost_micros
from llm.serialization import blocks_from_json, blocks_to_json
from llm.service import LlmService, get_llm_service
from memory import services as memory

log = get_logger(__name__)

MAX_DEPTH = 2
MAX_CHILDREN = 5
REPEAT_LIMIT = 3


def start_run(
    *,
    workspace_id: uuid.UUID,
    agent_name: str,
    goal: str,
    requested_by: str,
    parent: AgentRun | None = None,
    dispatch: bool = True,
) -> AgentRun:
    ensure_default_agents(workspace_id)
    agent = AgentDefinition.objects.filter(
        workspace_id=workspace_id, name=agent_name, enabled=True
    ).first()
    if agent is None:
        raise NotFoundError(f"agent {agent_name!r} is not available")
    depth = (parent.depth + 1) if parent else 0
    if depth > MAX_DEPTH:
        raise DomainError("delegation is too deep")
    run = AgentRun.objects.create(
        workspace_id=workspace_id,
        agent=agent,
        parent=parent,
        depth=depth,
        goal=goal,
        requested_by=requested_by,
        tainted=bool(parent and parent.tainted),
    )
    if dispatch:
        from agents.tasks import execute_run_task

        transaction.on_commit(lambda: execute_run_task.delay(str(run.pk)))
    return run


def cancel_run(run: AgentRun) -> bool:
    updated = AgentRun.objects.filter(
        pk=run.pk, status__in=[RunStatus.QUEUED, RunStatus.RUNNING]
    ).update(status=RunStatus.CANCELLED, finished_at=timezone.now())
    return bool(updated)


def _system_prompt(run: AgentRun) -> tuple[str, str]:
    agent = run.agent
    prompt = prompts.get_prompt(agent.prompt_key)
    hits = memory.retrieve(
        workspace_id=run.workspace_id,
        query=run.goal,
        channel=agent.channel or None,
        agent=agent.name,
    )
    context = {
        "workspace_name": run.workspace.name,
        "brand_profile": memory.render_brand_profile(
            memory.current_brand_profile(run.workspace_id)
        ),
        "memories": memory.render_memories(hits),
        "today": date.today().isoformat(),  # noqa: DTZ011 - a calendar date for the prompt
        "platforms": "Meta, Google Ads, OpenAI Ads",
    }
    values = {name: context[name] for name in prompt.variables}
    return prompt.render(**values), f"{prompt.key}@v{prompt.version}"


def _history(run: AgentRun) -> list[Message]:
    messages: list[Message] = []
    for step in run.steps.order_by("index"):
        role = step.payload["role"]
        messages.append(Message(role, blocks_from_json(step.payload["content"])))
    return messages


def _record(
    run: AgentRun, kind: str, role: str, content: list[dict[str, object]], **extra: int
) -> None:
    AgentStep.objects.create(
        workspace_id=run.workspace_id,
        run=run,
        index=run.steps.count(),
        kind=kind,
        payload={"role": role, "content": content},
        **extra,
    )


def _finish(run: AgentRun, status: RunStatus, *, text: str = "", error: str = "") -> AgentRun:
    pending = list(
        ProposedAction.objects.filter(run_id=run.pk, status="awaiting_approval").values_list(
            "pk", flat=True
        )
    )
    run.status = status
    run.error = error[:2000]
    run.result = {"text": text, "pending_actions": [str(p) for p in pending]}
    run.finished_at = timezone.now()
    run.save()
    if run.parent_id and run.tainted:
        AgentRun.objects.filter(pk=run.parent_id).update(tainted=True)
    return run


def _signature(call: ToolUseBlock) -> str:
    return f"{call.name}:{json.dumps(call.input, sort_keys=True, default=str)}"


def run_agent(
    run_id: uuid.UUID,
    *,
    service: LlmService | None = None,
    tools: ToolRegistry | None = None,
) -> AgentRun:
    tools = tools or default_registry
    run = AgentRun.objects.select_related("agent", "workspace").get(pk=run_id)
    service = service or get_llm_service(run.workspace_id)
    if run.status in TERMINAL_STATUSES:
        return run
    agent = run.agent
    started = time.monotonic()
    if run.status == RunStatus.QUEUED:
        run.status = RunStatus.RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at", "updated_at"])
        _record(run, "goal", "user", [{"type": "text", "text": run.goal}])

    system_text, prompt_key = _system_prompt(run)
    system = [SystemBlock(system_text, cache=True)]
    specs = tools.specs_for(agent.tools)
    messages = _history(run)
    recent: list[str] = []
    ctx = ToolContext(run=run, agent_name=agent.name, service=service, registry=tools)

    for _ in range(agent.max_steps):
        if AgentRun.objects.filter(pk=run.pk, status=RunStatus.CANCELLED).exists():
            run.refresh_from_db()
            return run
        if agent.max_cost_micros and run.cost_micros >= agent.max_cost_micros:
            return _finish(run, RunStatus.BUDGET_EXHAUSTED, error="cost budget reached")
        if time.monotonic() - started > agent.max_wall_seconds:
            return _finish(run, RunStatus.BUDGET_EXHAUSTED, error="time budget reached")

        request = LlmRequest(
            tier=agent.tier, system=system, messages=list(messages), tools=list(specs)
        )
        try:
            response = service.complete(
                request, workspace_id=run.workspace_id, run_id=run.pk, prompt_key=prompt_key
            )
        except DomainError as exc:
            log.warning("agent_llm_failed", run=str(run.pk), code=exc.code.value)
            return _finish(run, RunStatus.FAILED, error=f"{exc.code.value}: {exc.detail}")

        cost, _known = cost_micros(response.model, response.usage)
        _account(run, response, cost)
        _record(
            run, "llm", "assistant", blocks_to_json(response.content),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_micros=cost,
        )  # fmt: skip
        messages.append(Message("assistant", response.content))

        if response.stop_reason != "tool_use":
            if response.stop_reason in {"max_tokens", "refusal"}:
                return _finish(
                    run,
                    RunStatus.FAILED,
                    text=response.text,
                    error=f"stopped: {response.stop_reason}",
                )
            return _finish(run, RunStatus.SUCCEEDED, text=response.text)

        results: list[ToolResultBlock] = []
        looped = False
        for call in response.tool_uses:
            recent.append(_signature(call))
            if len(recent) >= REPEAT_LIMIT and len(set(recent[-REPEAT_LIMIT:])) == 1:
                results.append(ToolResultBlock(call.id, "Repeated identical call; stopping.", True))
                looped = True
                continue
            outcome = execute_tool(tools, agent.tools, ctx, call.name, call.input)
            results.append(ToolResultBlock(call.id, outcome.text, outcome.is_error))
        _record(run, "tool", "user", blocks_to_json(list(results)))
        messages.append(Message("user", list(results)))
        if looped:
            return _finish(
                run, RunStatus.FAILED, error="loop detected: repeated identical tool call"
            )

    return _finish(run, RunStatus.BUDGET_EXHAUSTED, error="step budget reached")


def _account(run: AgentRun, response: LlmResponse, cost: int) -> None:
    run.steps_count += 1
    run.input_tokens += response.usage.input_tokens
    run.output_tokens += response.usage.output_tokens
    run.cost_micros += cost
    run.save(
        update_fields=["steps_count", "input_tokens", "output_tokens", "cost_micros", "updated_at"]
    )
