"""The runtime: enrol contacts, run steps one at a time, keep waits in the database.

Everything that must survive a crash is a column: the current step, when to wake, what to wait
for, how many tries the step has had. A step and its log line commit together, so a worker that
dies between steps loses nothing, and a step that sends carries a stable idempotency key.
"""

from __future__ import annotations

import traceback
import uuid
from datetime import timedelta
from typing import Any

from celery import current_app
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.errors import DomainError, NotFoundError
from core.models import DeadLetter
from crm.contacts import services as contacts
from crm.ops import metrics
from crm.workflows import actions
from crm.workflows.models import (
    OPEN_STATUSES,
    Enrollment,
    EnrollmentStatus,
    StepLog,
    StepOutcome,
    Workflow,
    WorkflowStatus,
)

RUN_TASK = "crm.workflows.run"
QUEUE = "default"
RETRY_CAP_SECONDS = 3600
RESUME_KEY = "resume"
RETRY_KEY = "retrying"
FINISHED = "finished"


def _dispatch(workspace_id: uuid.UUID, enrollment_id: uuid.UUID) -> None:
    current_app.send_task(
        RUN_TASK,
        kwargs={"workspace_id": str(workspace_id), "enrollment_id": str(enrollment_id)},
        queue=QUEUE,
    )


def _kick(enrollment: Enrollment) -> None:
    workspace_id, enrollment_id = enrollment.workspace_id, enrollment.pk
    transaction.on_commit(lambda: _dispatch(workspace_id, enrollment_id))


# ---- entering -----------------------------------------------------------------------------


def enroll(
    workspace_id: uuid.UUID,
    workflow_id: uuid.UUID,
    contact_id: uuid.UUID,
    *,
    source: str = "manual",
) -> Enrollment | None:
    """Put a contact into a workflow. Returns ``None`` when the rules say they may not enter."""
    contacts.get_contact(workspace_id, contact_id)
    with transaction.atomic():
        workflow = (
            Workflow.objects.select_for_update(of=("self",))
            .select_related("published_version")
            .filter(pk=workflow_id, workspace_id=workspace_id, archived_at__isnull=True)
            .first()
        )
        if (
            workflow is None
            or workflow.status != WorkflowStatus.ACTIVE
            or workflow.published_version is None
        ):
            return None
        past = Enrollment.objects.filter(workflow=workflow, contact_id=contact_id)
        if past.filter(status__in=OPEN_STATUSES).exists():
            return None
        if not workflow.reenter and past.exists():
            return None
        version = workflow.published_version
        enrollment = Enrollment.objects.create(
            workspace_id=workspace_id,
            workflow=workflow,
            version=version,
            contact_id=contact_id,
            current_step=str(version.definition["start"]),
            context={"source": source},
        )
        _kick(enrollment)
    return enrollment


def exit_enrollment(enrollment: Enrollment, reason: str) -> None:
    enrollment.status = EnrollmentStatus.EXITED
    enrollment.exit_reason = reason
    enrollment.wake_at = None
    enrollment.waiting_for = ""
    enrollment.finished_at = timezone.now()
    enrollment.save()


# ---- running ------------------------------------------------------------------------------


def _step(enrollment: Enrollment) -> dict[str, Any]:
    for step in enrollment.version.definition["steps"]:
        if step["id"] == enrollment.current_step:
            return dict(step)
    raise NotFoundError("step not found")


def _log(enrollment: Enrollment, step: dict[str, Any], outcome: str, detail: str = "") -> None:
    metrics.workflow_step(str(step["type"]), outcome)
    StepLog.objects.create(
        workspace_id=enrollment.workspace_id,
        enrollment=enrollment,
        step_id=step["id"],
        step_type=step["type"],
        outcome=outcome,
        detail=detail[:300],
        attempt=enrollment.attempts + 1,
    )


def _due_resume(enrollment: Enrollment, now: Any) -> str | None:
    """How a runnable enrollment got here, or ``None`` when it must keep waiting."""
    if enrollment.status == EnrollmentStatus.ACTIVE:
        return str(enrollment.context.pop(RESUME_KEY, ""))
    if enrollment.wake_at is None or enrollment.wake_at > now:
        return None
    if enrollment.context.pop(RETRY_KEY, False):
        return ""
    return "timeout" if enrollment.waiting_for else "timer"


def run(workspace_id: uuid.UUID, enrollment_id: uuid.UUID) -> str:
    """Run steps until the contact has to wait, finishes or the per-run limit is reached."""
    for _ in range(settings.CRM_WORKFLOW_RUN_STEP_LIMIT):
        with transaction.atomic():
            enrollment = (
                Enrollment.objects.select_for_update(skip_locked=True)
                .select_related("version", "workflow")
                .filter(pk=enrollment_id, workspace_id=workspace_id, status__in=OPEN_STATUSES)
                .first()
            )
            if enrollment is None:
                return "idle"
            resume = _due_resume(enrollment, timezone.now())
            if resume is None:
                return "waiting"
            enrollment.status = EnrollmentStatus.ACTIVE
            enrollment.wake_at, enrollment.waiting_for = None, ""
            _execute(enrollment, resume)
            if enrollment.status != EnrollmentStatus.ACTIVE:
                return str(enrollment.status)
    _kick_later(workspace_id, enrollment_id)
    return "yielded"


def _kick_later(workspace_id: uuid.UUID, enrollment_id: uuid.UUID) -> None:
    transaction.on_commit(lambda: _dispatch(workspace_id, enrollment_id))


def _execute(enrollment: Enrollment, resume: str) -> None:
    step = _step(enrollment)
    action = actions.get(str(step["type"]))
    try:
        if action is None:
            raise NotFoundError("unknown action")
        ctx = actions.StepContext(
            workspace_id=enrollment.workspace_id,
            enrollment=enrollment,
            config=action.config_model.model_validate(step.get("config") or {}),
            contact_id=enrollment.contact_id,
            resume=resume,
        )
        with transaction.atomic():
            result = action.execute(ctx)
    except Exception as exc:
        _step_failed(enrollment, step, exc)
        return
    _apply(enrollment, step, result)


def _apply(enrollment: Enrollment, step: dict[str, Any], result: actions.StepResult) -> None:
    now = timezone.now()
    kind = result.kind
    if kind in ("wait", "wait_event"):
        enrollment.status = EnrollmentStatus.WAITING
        enrollment.wake_at = now + timedelta(seconds=result.seconds)
        enrollment.waiting_for = result.event if kind == "wait_event" else ""
        _log(enrollment, step, StepOutcome.WAITING, result.event or f"{result.seconds}s")
    elif kind == "stay":
        enrollment.attempts += 1
        if enrollment.attempts >= settings.CRM_WORKFLOW_MAX_ATTEMPTS:
            _log(enrollment, step, StepOutcome.SKIPPED, f"gave up: {result.detail}")
            _advance(enrollment, step, None)
        else:
            enrollment.status = EnrollmentStatus.WAITING
            enrollment.wake_at = now + timedelta(seconds=result.seconds)
            enrollment.context[RETRY_KEY] = True
            _log(enrollment, step, StepOutcome.RETRY, result.detail)
    elif kind == "stop":
        done = result.detail == FINISHED
        _log(enrollment, step, StepOutcome.DONE if done else StepOutcome.SKIPPED, result.detail)
        enrollment.status = EnrollmentStatus.COMPLETED if done else EnrollmentStatus.EXITED
        enrollment.exit_reason = "" if done else result.detail
        enrollment.finished_at = now
    elif kind == "skip":
        _log(enrollment, step, StepOutcome.SKIPPED, result.detail)
        _advance(enrollment, step, None)
    else:
        _log(enrollment, step, StepOutcome.DONE, result.detail)
        _advance(enrollment, step, result.goto if kind == "goto" else None)
    enrollment.save()


def _advance(enrollment: Enrollment, step: dict[str, Any], goto: str | None) -> None:
    target = goto or step.get("next")
    enrollment.attempts = 0
    if target:
        enrollment.current_step = str(target)
        return
    enrollment.status = EnrollmentStatus.COMPLETED
    enrollment.finished_at = timezone.now()


def _step_failed(enrollment: Enrollment, step: dict[str, Any], exc: Exception) -> None:
    """Retry with exponential backoff; after the last try park the failure in the dead letters."""
    enrollment.attempts += 1
    # Only the kind of error is logged: messages can carry customer data.
    label = getattr(exc, "code", None) or type(exc).__name__
    if enrollment.attempts >= settings.CRM_WORKFLOW_MAX_ATTEMPTS:
        _log(enrollment, step, StepOutcome.FAILED, str(label))
        enrollment.status = EnrollmentStatus.FAILED
        enrollment.exit_reason = "step_failed"
        enrollment.finished_at = timezone.now()
        _dead_letter(enrollment, step, exc)
    else:
        delay = min(
            settings.CRM_WORKFLOW_RETRY_BASE_SECONDS * 2 ** (enrollment.attempts - 1),
            RETRY_CAP_SECONDS,
        )
        _log(enrollment, step, StepOutcome.RETRY, str(label))
        enrollment.status = EnrollmentStatus.WAITING
        enrollment.wake_at = timezone.now() + timedelta(seconds=delay)
        enrollment.context[RETRY_KEY] = True
    enrollment.save()


def _dead_letter(enrollment: Enrollment, step: dict[str, Any], exc: Exception) -> None:
    kind = exc.__class__.__name__ if not isinstance(exc, DomainError) else str(exc.code)
    DeadLetter.objects.create(
        task_name="crm.workflows.step",
        kwargs={
            "workspace_id": str(enrollment.workspace_id),
            "enrollment_id": str(enrollment.pk),
            "step_id": step["id"],
        },
        error=f"{kind}"[:2000],
        traceback="".join(traceback.format_exception(exc))[-8000:],
    )


# ---- waking -------------------------------------------------------------------------------


def due_enrollments(limit: int = 500) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(workspace, enrollment) pairs whose wait is over or that were never picked up."""
    now = timezone.now()
    lost = now - timedelta(seconds=settings.CRM_WORKFLOW_LOST_AFTER_SECONDS)
    rows = Enrollment.objects.filter(
        Q(status__in=OPEN_STATUSES, wake_at__lte=now)
        | Q(status=EnrollmentStatus.ACTIVE, updated_at__lte=lost)
    )
    return list(rows.values_list("workspace_id", "pk")[:limit])


def wake_due() -> int:
    due = due_enrollments()
    for workspace_id, enrollment_id in due:
        _dispatch(workspace_id, enrollment_id)
    return len(due)


# ---- events -------------------------------------------------------------------------------


def on_inbound_message(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> None:
    """A customer wrote. Waits for a reply continue; the others stop if the workflow says so."""
    open_rows = (
        Enrollment.objects.select_for_update()
        .filter(workspace_id=workspace_id, contact_id=contact_id, status__in=OPEN_STATUSES)
        .select_related("workflow")
    )
    with transaction.atomic():
        for enrollment in open_rows:
            if enrollment.waiting_for == actions.REPLY_EVENT:
                enrollment.status = EnrollmentStatus.ACTIVE
                enrollment.waiting_for, enrollment.wake_at = "", None
                enrollment.context[RESUME_KEY] = "event"
                enrollment.save()
                _kick(enrollment)
            elif enrollment.workflow.stop_on_reply:
                exit_enrollment(enrollment, "replied")


def exit_contact(workspace_id: uuid.UUID, contact_id: uuid.UUID, reason: str) -> int:
    rows = Enrollment.objects.filter(
        workspace_id=workspace_id, contact_id=contact_id, status__in=OPEN_STATUSES
    )
    count = 0
    for enrollment in rows:
        exit_enrollment(enrollment, reason)
        count += 1
    return count
