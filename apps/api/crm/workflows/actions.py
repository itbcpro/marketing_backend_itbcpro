"""Actions: what a step does. One class per action, registered by key.

An action reports what should happen next instead of doing the scheduling itself, so the engine
alone decides how waits are stored and how failures are retried.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar, Literal

from django.conf import settings
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field

from audit.actors import Actor
from crm.activity import services as activity
from crm.consent.models import Purpose
from crm.contacts import services as contacts
from crm.sending import services as sending
from crm.templates import services as templates
from crm.workflows.conditions import CUSTOM_PREFIX, Condition, evaluate
from crm.workflows.models import Enrollment

SYSTEM = Actor.system("workflow")
SECONDS_PER_DAY = 86_400
RETRY_BLOCKS = frozenset({"quiet_hours", "rate_limited"})
REPLY_EVENT = "reply"
EVENTS = (REPLY_EVENT,)


@dataclass(frozen=True)
class StepResult:
    kind: Literal["next", "goto", "wait", "wait_event", "stay", "stop", "skip"] = "next"
    seconds: int = 0
    event: str = ""
    goto: str = ""
    detail: str = ""


@dataclass(frozen=True)
class StepContext:
    workspace_id: uuid.UUID
    enrollment: Enrollment
    config: BaseModel
    contact_id: uuid.UUID
    resume: str  # "", "event" or "timeout": how a wait came to an end


class Action:
    key: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]

    def targets(self, config: BaseModel) -> list[str]:
        """Step ids this step may jump to besides its ``next``; checked when publishing."""
        return []

    def execute(self, ctx: StepContext) -> StepResult:
        raise NotImplementedError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SendMessage(Action):
    key = "send_message"

    class Config(_Strict):
        channel: str
        body: str = ""
        subject: str = ""
        template_id: uuid.UUID | None = None
        purpose: Literal["transactional", "marketing"] = "transactional"
        on_blocked: Literal["continue", "stop"] = "continue"

    config_model = Config

    def execute(self, ctx: StepContext) -> StepResult:
        cfg: SendMessage.Config = ctx.config  # type: ignore[assignment]
        subject, body = cfg.subject, cfg.body
        if cfg.template_id:
            row = templates.get(ctx.workspace_id, cfg.template_id)
            subject, body = row.subject, row.body
        rendered = templates.render_text(
            ctx.workspace_id, ctx.contact_id, subject=subject, body=body
        )
        outcome = sending.send(
            workspace_id=ctx.workspace_id,
            actor=SYSTEM,
            contact_id=ctx.contact_id,
            channel=cfg.channel,
            body=rendered.body,
            subject=rendered.subject,
            purpose=Purpose(cfg.purpose),
            # The same step of the same enrollment always has the same key: a retry after a
            # crash finds the first send instead of sending twice.
            idempotency_key=f"wf:{ctx.enrollment.pk}:{ctx.enrollment.current_step}",
        )
        if not outcome.blocked:
            return StepResult()
        if outcome.reason in RETRY_BLOCKS:
            return StepResult(
                kind="stay",
                seconds=settings.CRM_WORKFLOW_BLOCKED_RETRY_MINUTES * 60,
                detail=outcome.reason,
            )
        return StepResult(
            kind="stop" if cfg.on_blocked == "stop" else "skip", detail=outcome.reason
        )


class AddTag(Action):
    key = "add_tag"

    class Config(_Strict):
        tag: str

    config_model = Config

    def execute(self, ctx: StepContext) -> StepResult:
        cfg: AddTag.Config = ctx.config  # type: ignore[assignment]
        contact = contacts.get_contact(ctx.workspace_id, ctx.contact_id)
        contacts.add_tags(
            workspace_id=ctx.workspace_id, actor=SYSTEM, contact=contact, names=[cfg.tag]
        )
        return StepResult()


class RemoveTag(Action):
    key = "remove_tag"
    config_model = AddTag.Config

    def execute(self, ctx: StepContext) -> StepResult:
        cfg: AddTag.Config = ctx.config  # type: ignore[assignment]
        contact = contacts.get_contact(ctx.workspace_id, ctx.contact_id)
        contacts.remove_tags(
            workspace_id=ctx.workspace_id, actor=SYSTEM, contact=contact, names=[cfg.tag]
        )
        return StepResult()


class CreateTask(Action):
    key = "create_task"

    class Config(_Strict):
        title: str
        due_in_days: int | None = Field(default=None, ge=0, le=365)

    config_model = Config

    def execute(self, ctx: StepContext) -> StepResult:
        cfg: CreateTask.Config = ctx.config  # type: ignore[assignment]
        contact = contacts.get_contact(ctx.workspace_id, ctx.contact_id)
        due = (
            timezone.now() + timedelta(days=cfg.due_in_days)
            if cfg.due_in_days is not None
            else None
        )
        activity.create_task(
            workspace_id=ctx.workspace_id,
            actor=SYSTEM,
            contact_id=ctx.contact_id,
            title=cfg.title,
            due_at=due,
            assigned_to=contact.assigned_to_id,
        )
        return StepResult()


class AssignOwner(Action):
    key = "assign_owner"

    class Config(_Strict):
        user_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)

    config_model = Config

    def execute(self, ctx: StepContext) -> StepResult:
        cfg: AssignOwner.Config = ctx.config  # type: ignore[assignment]
        if len(cfg.user_ids) == 1:
            contacts.assign(
                workspace_id=ctx.workspace_id,
                actor=SYSTEM,
                contact_id=ctx.contact_id,
                user_id=cfg.user_ids[0],
            )
        else:
            contacts.assign_round_robin(
                workspace_id=ctx.workspace_id,
                actor=SYSTEM,
                contact_id=ctx.contact_id,
                user_ids=cfg.user_ids,
            )
        return StepResult()


class SetField(Action):
    key = "set_field"

    class Config(_Strict):
        field: str
        value: str

    config_model = Config

    def execute(self, ctx: StepContext) -> StepResult:
        cfg: SetField.Config = ctx.config  # type: ignore[assignment]
        if cfg.field.startswith(CUSTOM_PREFIX):
            data: dict[str, Any] = {"custom": {cfg.field[len(CUSTOM_PREFIX) :]: cfg.value}}
        else:
            data = {cfg.field: cfg.value}
        contacts.update_contact(
            workspace_id=ctx.workspace_id, actor=SYSTEM, contact_id=ctx.contact_id, data=data
        )
        return StepResult()


class Wait(Action):
    key = "wait"

    class Config(_Strict):
        seconds: int = Field(ge=1)

    config_model = Config

    def execute(self, ctx: StepContext) -> StepResult:
        cfg: Wait.Config = ctx.config  # type: ignore[assignment]
        if ctx.resume:  # woken after the wait: carry on
            return StepResult()
        limit = settings.CRM_WORKFLOW_MAX_WAIT_DAYS * SECONDS_PER_DAY
        return StepResult(kind="wait", seconds=min(cfg.seconds, limit))


class WaitForEvent(Action):
    key = "wait_for_event"

    class Config(_Strict):
        event: Literal["reply"] = "reply"
        timeout_seconds: int = Field(ge=1)
        on_event: str = ""
        on_timeout: str = ""

    config_model = Config

    def targets(self, config: BaseModel) -> list[str]:
        cfg: WaitForEvent.Config = config  # type: ignore[assignment]
        return [t for t in (cfg.on_event, cfg.on_timeout) if t]

    def execute(self, ctx: StepContext) -> StepResult:
        cfg: WaitForEvent.Config = ctx.config  # type: ignore[assignment]
        if ctx.resume == "event":
            return StepResult(kind="goto", goto=cfg.on_event) if cfg.on_event else StepResult()
        if ctx.resume == "timeout":
            return StepResult(kind="goto", goto=cfg.on_timeout) if cfg.on_timeout else StepResult()
        limit = settings.CRM_WORKFLOW_MAX_WAIT_DAYS * SECONDS_PER_DAY
        return StepResult(
            kind="wait_event", event=cfg.event, seconds=min(cfg.timeout_seconds, limit)
        )


class Branch(Action):
    key = "branch"

    class Config(_Strict):
        rules: list[Condition] = Field(min_length=1, max_length=10)
        match: Literal["all", "any"] = "all"
        yes: str
        no: str

    config_model = Config

    def targets(self, config: BaseModel) -> list[str]:
        cfg: Branch.Config = config  # type: ignore[assignment]
        return [cfg.yes, cfg.no]

    def execute(self, ctx: StepContext) -> StepResult:
        cfg: Branch.Config = ctx.config  # type: ignore[assignment]
        contact = contacts.get_contact(ctx.workspace_id, ctx.contact_id)
        row = contacts.serialise_contacts(ctx.workspace_id, [contact])[0]
        passed = evaluate(cfg.rules, cfg.match, row)
        return StepResult(kind="goto", goto=cfg.yes if passed else cfg.no)


class Finish(Action):
    key = "finish"
    config_model = _Strict

    def execute(self, ctx: StepContext) -> StepResult:
        return StepResult(kind="stop", detail="finished")


_actions: dict[str, Action] = {}


def register(action: Action) -> None:
    _actions[action.key] = action


def get(key: str) -> Action | None:
    return _actions.get(key)


def all_actions() -> list[Action]:
    return sorted(_actions.values(), key=lambda a: a.key)


for _cls in (
    SendMessage,
    AddTag,
    RemoveTag,
    CreateTask,
    AssignOwner,
    SetField,
    Wait,
    WaitForEvent,
    Branch,
    Finish,
):
    register(_cls())
