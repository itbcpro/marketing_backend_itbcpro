"""Workflow definitions: validation, drafts and publishing."""

from __future__ import annotations

import re
import uuid
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from pydantic import ValidationError

from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.plans import services as plans
from crm.workflows import actions, triggers
from crm.workflows.models import Workflow, WorkflowStatus, WorkflowVersion

NAME_LIMIT = 100
ON_PATH, FINISHED = 1, 2  # depth-first search marks for the cycle check
STEP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,59}$")


def _errors_of(exc: ValidationError, prefix: str) -> dict[str, str]:
    return {
        f"{prefix}.{'.'.join(str(p) for p in e['loc'])}".rstrip("."): str(e["type"])
        for e in exc.errors()
    }


def validate(definition: dict[str, Any]) -> dict[str, Any]:
    """Return the definition when it can run, else raise with a code for each problem."""
    errors: dict[str, str] = {}
    trigger = definition.get("trigger") or {}
    kind = triggers.get(str(trigger.get("type", "")))
    if kind is None:
        errors["trigger.type"] = "unknown_trigger"
    else:
        try:
            kind.config_model.model_validate(trigger.get("config") or {})
        except ValidationError as exc:
            errors |= _errors_of(exc, "trigger.config")
    steps = definition.get("steps") or []
    if not isinstance(steps, list) or not steps:
        errors["steps"] = "required"
    elif len(steps) > settings.CRM_WORKFLOW_MAX_STEPS:
        errors["steps"] = "too_many"
    else:
        errors |= _check_steps(steps, str(definition.get("start", "")))
    if errors:
        raise ValidationFailedError("invalid workflow", errors=errors)
    return definition


def _check_steps(steps: list[dict[str, Any]], start: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    ids = [str(s.get("id", "")) for s in steps]
    if len(set(ids)) != len(ids) or not all(STEP_ID.match(i) for i in ids):
        errors["steps"] = "invalid_or_duplicate_ids"
        return errors
    if start not in ids:
        errors["start"] = "unknown_step"
    edges: dict[str, list[str]] = {}
    for step in steps:
        sid = str(step["id"])
        action = actions.get(str(step.get("type", "")))
        if action is None:
            errors[f"steps.{sid}.type"] = "unknown_action"
            continue
        try:
            config = action.config_model.model_validate(step.get("config") or {})
        except ValidationError as exc:
            errors |= _errors_of(exc, f"steps.{sid}.config")
            continue
        following = [str(step["next"])] if step.get("next") else []
        edges[sid] = following + action.targets(config)
        for target in edges[sid]:
            if target not in ids:
                errors[f"steps.{sid}"] = "unknown_target"
    if not errors and _has_cycle(edges):
        errors["steps"] = "cycle"
    return errors


def _has_cycle(edges: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == ON_PATH:
            return True
        if state.get(node) == FINISHED:
            return False
        state[node] = ON_PATH
        found = any(visit(n) for n in edges.get(node, []))
        state[node] = FINISHED
        return found

    return any(visit(n) for n in edges)


# ---- workflows -----------------------------------------------------------------------------


def count_published(workspace_id: uuid.UUID) -> int:
    """Automations that have a published version (running or paused)."""
    return (
        Workflow.objects.for_workspace(workspace_id)
        .filter(archived_at__isnull=True, published_version__isnull=False)
        .count()
    )


def list_workflows(workspace_id: uuid.UUID) -> list[Workflow]:
    rows = Workflow.objects.for_workspace(workspace_id).filter(archived_at__isnull=True)
    return list(rows.select_related("published_version"))


def get_workflow(workspace_id: uuid.UUID, workflow_id: uuid.UUID) -> Workflow:
    found = Workflow.objects.for_workspace(workspace_id).filter(pk=workflow_id).first()
    if found is None or found.archived_at is not None:
        raise NotFoundError("workflow not found")
    return found


def create_workflow(
    workspace_id: uuid.UUID, *, name: str, reenter: bool = False, stop_on_reply: bool = True
) -> Workflow:
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > NAME_LIMIT:
        raise ValidationFailedError("invalid_name")
    try:
        with transaction.atomic():
            return Workflow.objects.create(
                workspace_id=workspace_id,
                name=cleaned,
                reenter=reenter,
                stop_on_reply=stop_on_reply,
            )
    except IntegrityError as exc:
        raise ConflictError("workflow name already exists") from exc


def update_settings(
    workspace_id: uuid.UUID,
    workflow_id: uuid.UUID,
    *,
    name: str | None = None,
    reenter: bool | None = None,
    stop_on_reply: bool | None = None,
) -> Workflow:
    workflow = get_workflow(workspace_id, workflow_id)
    if name is not None:
        cleaned = " ".join(name.split())
        if not cleaned or len(cleaned) > NAME_LIMIT:
            raise ValidationFailedError("invalid_name")
        workflow.name = cleaned
    if reenter is not None:
        workflow.reenter = reenter
    if stop_on_reply is not None:
        workflow.stop_on_reply = stop_on_reply
    try:
        with transaction.atomic():
            workflow.save()
    except IntegrityError as exc:
        raise ConflictError("workflow name already exists") from exc
    return workflow


def latest_version(workflow: Workflow) -> WorkflowVersion | None:
    return workflow.versions.order_by("-number").first()


def save_draft(
    workspace_id: uuid.UUID, workflow_id: uuid.UUID, definition: dict[str, Any]
) -> WorkflowVersion:
    """Store a definition as the draft. A published version is never edited: it gets a successor."""
    workflow = get_workflow(workspace_id, workflow_id)  # a stranger learns nothing else
    validate(definition)
    with transaction.atomic():
        Workflow.objects.select_for_update().get(pk=workflow.pk)
        latest = latest_version(workflow)
        if latest is not None and latest.published_at is None:
            latest.definition = definition
            latest.save(update_fields=["definition", "updated_at"])
            return latest
        return WorkflowVersion.objects.create(
            workspace_id=workspace_id,
            workflow=workflow,
            number=(latest.number + 1) if latest else 1,
            definition=definition,
        )


def publish(workspace_id: uuid.UUID, workflow_id: uuid.UUID) -> Workflow:
    workflow = get_workflow(workspace_id, workflow_id)
    with transaction.atomic():
        Workflow.objects.select_for_update().get(pk=workflow.pk)
        draft = latest_version(workflow)
        if draft is None or draft.published_at is not None:
            raise ConflictError("nothing to publish")
        validate(draft.definition)
        if workflow.published_version_id is None:
            plans.ensure_room(workspace_id, plans.WORKFLOWS, lambda: count_published(workspace_id))
        draft.published_at = timezone.now()
        draft.save(update_fields=["published_at", "updated_at"])
        workflow.published_version = draft
        workflow.status = WorkflowStatus.ACTIVE
        workflow.save(update_fields=["published_version", "status", "updated_at"])
    return workflow


def set_paused(workspace_id: uuid.UUID, workflow_id: uuid.UUID, paused: bool) -> Workflow:
    workflow = get_workflow(workspace_id, workflow_id)
    if workflow.published_version_id is None:
        raise ConflictError("publish a version first")
    workflow.status = WorkflowStatus.PAUSED if paused else WorkflowStatus.ACTIVE
    workflow.save(update_fields=["status", "updated_at"])
    return workflow


def archive(workspace_id: uuid.UUID, workflow_id: uuid.UUID) -> None:
    workflow = get_workflow(workspace_id, workflow_id)
    workflow.archived_at = timezone.now()
    workflow.status = WorkflowStatus.PAUSED
    workflow.save(update_fields=["archived_at", "status", "updated_at"])
