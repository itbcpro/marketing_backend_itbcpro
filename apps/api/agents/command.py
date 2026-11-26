"""Command box: a free-text request becomes a structured brief, questions, a plan and a run."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from actions.models import Guardrail
from agents import runtime
from agents.models import AgentRun, CommandSession
from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError
from identity.models import User
from llm import prompts
from llm.ports import LlmRequest, Message, SystemBlock
from llm.service import LlmService
from llm.structured import complete_structured
from memory import services as memory
from workspaces.models import Workspace

PROMPT_KEY = "command/brief"
MAX_ROUNDS = 4
ORCHESTRATOR = "orchestrator"

Intent = Literal["campaign", "content", "social", "research", "report", "question", "other"]


class MissingField(BaseModel):
    field: str
    question: str


class Brief(BaseModel):
    intent: Intent
    summary: str
    platforms: list[str] = Field(default_factory=list)
    market: str | None = None
    content_language: str | None = None
    objective: str | None = None
    daily_budget_micros: int | None = None
    currency: str | None = None
    audience: str | None = None
    offer: str | None = None
    missing: list[MissingField] = Field(default_factory=list)


class PlanStep(BaseModel):
    agent: str
    what: str
    needs_approval: bool


class PlanPreview(BaseModel):
    steps: list[PlanStep]
    spend_note: str
    warnings: list[str] = Field(default_factory=list)


def _defaults(workspace: Workspace) -> str:
    lines = [
        f"Reporting currency: {workspace.reporting_currency}",
        f"Time zone: {workspace.time_zone}",
    ]
    lines.extend(
        f"Guardrail {g.key}: {g.config}"
        for g in Guardrail.objects.filter(workspace=workspace, enabled=True)
    )
    return "\n".join(lines)


def enforce_required_fields(brief: Brief) -> Brief:
    """Rules the model may not skip: money is never assumed."""
    asked = {m.field for m in brief.missing}
    if brief.intent == "campaign":
        if brief.daily_budget_micros is None and "budget" not in asked:
            brief.missing.append(
                MissingField(field="budget", question="What daily budget should the campaign use?")
            )
        if not brief.platforms and "platforms" not in asked:
            brief.missing.append(
                MissingField(
                    field="platforms", question="Which advertising platforms should it run on?"
                )
            )
    return brief


def interpret(workspace: Workspace, text: str, service: LlmService) -> Brief:
    prompt = prompts.get_prompt(PROMPT_KEY)
    system = prompt.render(
        request=text,
        workspace_name=workspace.name,
        brand_profile=memory.render_brand_profile(memory.current_brand_profile(workspace.pk)),
        known_defaults=_defaults(workspace),
    )
    request = LlmRequest(
        tier=prompt.tier,
        system=[SystemBlock(system)],
        messages=[Message.user("Extract the brief.")],
    )
    brief, _ = complete_structured(
        service,
        request,
        Brief,
        workspace_id=workspace.pk,
        prompt_key=f"{PROMPT_KEY}@v{prompt.version}",
    )
    return enforce_required_fields(brief)


def build_plan(brief: Brief, workspace: Workspace) -> PlanPreview:
    """Deterministic preview of what will happen, shown before anything is prepared."""
    steps: list[PlanStep] = []
    warnings: list[str] = []
    if brief.intent == "campaign":
        steps = [
            PlanStep(
                agent="ads-strategist",
                what="Draft campaign structure, targeting and copy",
                needs_approval=False,
            ),
            PlanStep(
                agent="ads-strategist",
                what="Create the campaign paused on each platform",
                needs_approval=True,
            ),
            PlanStep(agent="ads-strategist", what="Activate the campaign", needs_approval=True),
        ]
        cap = Guardrail.objects.filter(
            workspace=workspace, key="max_single_spend_micros", enabled=True
        ).first()
        if (
            cap
            and brief.daily_budget_micros
            and brief.daily_budget_micros > int(cap.config.get("amount", 0) or 0) > 0
        ):
            warnings.append(
                "The requested budget exceeds the single-action spend cap and will be blocked."
            )
    elif brief.intent in {"content", "social"}:
        steps = [
            PlanStep(
                agent=ORCHESTRATOR, what="Prepare drafts in the brand voice", needs_approval=False
            ),
            PlanStep(agent=ORCHESTRATOR, what="Publish after your approval", needs_approval=True),
        ]
    else:
        steps = [
            PlanStep(agent=ORCHESTRATOR, what="Research and report back", needs_approval=False)
        ]
    spend = (
        "No spend involved."
        if not brief.daily_budget_micros
        else (
            f"Daily budget {brief.daily_budget_micros / 1_000_000:.2f} "
            f"{brief.currency or workspace.reporting_currency}; "
            "nothing is spent until you approve activation."
        )
    )
    return PlanPreview(steps=steps, spend_note=spend, warnings=warnings)


def _goal_text(session: CommandSession) -> str:
    return (
        "Owner request (verbatim):\n"
        f"{session.text}\n\nClarifications:\n"
        + ("\n".join(f"- {a}" for a in session.answers) or "(none)")
        + f"\n\nStructured brief:\n{session.brief}"
    )


def submit(workspace: Workspace, user: User, text: str, service: LlmService) -> CommandSession:
    brief = interpret(workspace, text, service)
    session = CommandSession.objects.create(
        workspace=workspace,
        created_by=user,
        text=text,
        brief=brief.model_dump(mode="json"),
        status="needs_input" if brief.missing else "ready",
    )
    record_event(
        action="command.submitted",
        actor=Actor.human(user.pk),
        workspace_id=workspace.pk,
        target_type="command",
        target_id=session.pk,
        after={"intent": brief.intent, "missing": [m.field for m in brief.missing]},
    )
    return session


def answer(session: CommandSession, answers: dict[str, str], service: LlmService) -> CommandSession:
    if session.status not in {"needs_input", "ready"}:
        raise ConflictError("this command can no longer be changed")
    if len(session.answers) >= MAX_ROUNDS * 5:
        raise ConflictError("too many clarification rounds")
    session.answers = [*session.answers, *(f"{k}: {v}" for k, v in answers.items())]
    combined = session.text + "\n\nClarifications:\n" + "\n".join(session.answers)
    brief = interpret(session.workspace, combined, service)
    # Anything the owner just answered is no longer missing, even if the model asks again.
    answered = {k.lower() for k in answers}
    brief.missing = [m for m in brief.missing if m.field.lower() not in answered]
    session.brief = brief.model_dump(mode="json")
    session.status = "needs_input" if brief.missing else "ready"
    session.save()
    return session


def confirm(session: CommandSession, user: User) -> AgentRun:
    if session.status != "ready":
        raise ConflictError("the command still needs answers")
    run = runtime.start_run(
        workspace_id=session.workspace_id,
        agent_name=ORCHESTRATOR,
        goal=_goal_text(session),
        requested_by=f"user:{user.pk}",
    )
    session.run = run
    session.status = "running"
    session.save(update_fields=["run", "status", "updated_at"])
    record_event(
        action="command.confirmed",
        actor=Actor.human(user.pk),
        workspace_id=session.workspace_id,
        target_type="command",
        target_id=session.pk,
        after={"run_id": str(run.pk)},
    )
    return run


def session_id(value: str) -> uuid.UUID:
    return uuid.UUID(value)
