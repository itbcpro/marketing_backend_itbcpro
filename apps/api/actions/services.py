"""Action lifecycle: propose, validate, guard, approve, execute, verify, roll back.

This module is the only place that changes ``ProposedAction.status``; every legal transition is
listed in ``TRANSITIONS``.
"""

import hashlib
import uuid
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from pydantic import ValidationError

from actions import autonomy, guardrails
from actions.models import (
    ActionStatus,
    ExecutionAttempt,
    ProposedAction,
    RiskClass,
)
from actions.registry import ActionKind, get_action_kind
from audit import events
from audit.actors import Actor
from audit.services import record_event
from core.errors import (
    DomainError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
    RetryableProviderError,
    ValidationFailedError,
)
from core.logging import get_logger
from identity.models import User
from workspaces.permissions import Permission
from workspaces.services import require_permission

log = get_logger(__name__)

PROPOSAL_TTL = timedelta(days=2)
STUCK_AFTER = timedelta(minutes=15)

S = ActionStatus
TRANSITIONS: dict[str, frozenset[str]] = {
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.REJECTED, S.EXPIRED, S.POLICY_BLOCKED}),
    S.APPROVED: frozenset({S.EXECUTING, S.REJECTED}),
    S.EXECUTING: frozenset({S.SUCCEEDED, S.FAILED_RETRYABLE, S.FAILED_FINAL}),
    S.FAILED_RETRYABLE: frozenset({S.EXECUTING, S.FAILED_FINAL, S.REJECTED}),
    S.SUCCEEDED: frozenset({S.VERIFIED, S.ROLLED_BACK}),
    S.VERIFIED: frozenset({S.ROLLED_BACK}),
    S.POLICY_BLOCKED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.FAILED_FINAL: frozenset(),
    S.ROLLED_BACK: frozenset(),
}
APPROVAL_PERMISSION = {
    RiskClass.R1: Permission.APPROVE_R1,
    RiskClass.R2: Permission.APPROVE_R2,
    RiskClass.R3: Permission.APPROVE_R3,
}
REJECTION_REASONS = frozenset(
    {"tone", "factual", "brand", "targeting", "budget", "timing", "creative", "legal", "other"}
)


class ActionStateError(DomainError):
    code = ErrorCode.ACTION_STATE
    http_status = 409


class ActionBlockedError(DomainError):
    code = ErrorCode.ACTION_BLOCKED
    http_status = 422


def _move(action: ProposedAction, to: ActionStatus) -> None:
    if to not in TRANSITIONS[ActionStatus(action.status)]:
        raise ActionStateError(f"{action.status} -> {to} is not allowed")
    action.status = to


def _key_for(
    workspace_id: uuid.UUID, kind: str, payload: dict[str, Any], run_id: uuid.UUID | None
) -> str:
    digest = hashlib.sha256(
        f"{workspace_id}|{kind}|{run_id}|{guardrails.payload_fingerprint(payload)}".encode()
    ).hexdigest()
    return f"auto-{digest[:40]}"


def _validate(spec: ActionKind, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    try:
        model = spec.payload_model.model_validate(payload)
    except ValidationError as exc:
        raise ValidationFailedError(str(exc)[:500]) from exc
    spend = spec.spend_micros(model) if spec.spend_micros else 0
    return model.model_dump(mode="json"), int(spend)


def propose(
    *,
    workspace_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
    requested_by: str,
    run_id: uuid.UUID | None = None,
    tainted: bool = False,
    summary: str = "",
    idempotency_key: str | None = None,
    rollback_of: ProposedAction | None = None,
) -> ProposedAction:
    spec = get_action_kind(kind)
    clean, spend = _validate(spec, payload)
    key = idempotency_key or _key_for(workspace_id, kind, clean, run_id)
    existing = ProposedAction.objects.filter(workspace_id=workspace_id, idempotency_key=key).first()
    if existing:
        return existing
    try:
        with transaction.atomic():
            action = ProposedAction.objects.create(
                workspace_id=workspace_id,
                kind=kind,
                summary=summary[:300],
                payload=clean,
                risk=spec.risk,
                requested_by=requested_by,
                run_id=run_id,
                tainted=tainted,
                idempotency_key=key,
                spend_micros=spend,
                rollback_of=rollback_of,
                expires_at=timezone.now() + PROPOSAL_TTL,
            )
    except IntegrityError:
        return ProposedAction.objects.get(workspace_id=workspace_id, idempotency_key=key)

    with transaction.atomic():
        if spec.preview:
            action.before, action.after = spec.preview(action)
        violations = guardrails.evaluate(action, spec)
        action.violations = [v.as_dict() for v in violations]
        if violations:
            _move(action, ActionStatus.POLICY_BLOCKED)
            action.expires_at = None
        elif autonomy.may_auto_approve(action):
            action.auto_approved = True
            action.decided_at = timezone.now()
            _move(action, ActionStatus.APPROVED)
        action.save()
        record_event(
            action="action.proposed",
            actor=_actor_for(requested_by),
            workspace_id=workspace_id,
            target_type="action",
            target_id=action.pk,
            after={"kind": kind, "risk": spec.risk, "status": action.status, "spend_micros": spend},
        )
        events.publish(
            events.EventTopic.ACTION_PROPOSED,
            {
                "action_id": str(action.pk),
                "workspace_id": str(workspace_id),
                "status": action.status,
            },
        )
        if action.status == ActionStatus.APPROVED:
            _schedule_execution(action.pk)
    return action


def _actor_for(requested_by: str) -> Actor:
    return (
        Actor.human(requested_by.removeprefix("user:"))
        if requested_by.startswith("user:")
        else Actor.agent(requested_by)
    )


def _schedule_execution(action_id: uuid.UUID) -> None:
    from actions.tasks import execute_action_task

    transaction.on_commit(lambda: execute_action_task.delay(str(action_id)))


def get_action(workspace_id: uuid.UUID, action_id: uuid.UUID) -> ProposedAction:
    action = ProposedAction.objects.filter(pk=action_id, workspace_id=workspace_id).first()
    if action is None:
        raise NotFoundError("action not found")
    return action


def _authorise(user: User, action: ProposedAction) -> None:
    permission = APPROVAL_PERMISSION.get(RiskClass(action.risk))
    if permission is None:
        raise PermissionDeniedError()
    require_permission(user, action.workspace_id, permission)


def approve(
    *,
    workspace_id: uuid.UUID,
    action_id: uuid.UUID,
    user: User,
    note: str = "",
    edits: dict[str, Any] | None = None,
) -> ProposedAction:
    with transaction.atomic():
        action = (
            ProposedAction.objects.select_for_update()
            .filter(pk=action_id, workspace_id=workspace_id)
            .first()
        )
        if action is None:
            raise NotFoundError("action not found")
        _authorise(user, action)
        spec = get_action_kind(action.kind)
        if edits is not None:
            clean, spend = _validate(spec, edits)
            action.edited_payload = clean
            action.spend_micros = spend
        violations = guardrails.evaluate(action, spec)
        if violations:
            raise ActionBlockedError("; ".join(f"{v.rule}: {v.detail}" for v in violations))
        _move(action, ActionStatus.APPROVED)
        action.decided_by = user
        action.decided_at = timezone.now()
        action.decision_note = note
        action.save()
        record_event(
            action="action.approved",
            actor=Actor.human(user.pk),
            workspace_id=workspace_id,
            target_type="action",
            target_id=action.pk,
            after={"edited": edits is not None, "note": note[:200]},
        )
        events.publish(events.EventTopic.ACTION_DECIDED, _decision_payload(action, "approved"))
        _schedule_execution(action.pk)
    return action


def reject(
    *, workspace_id: uuid.UUID, action_id: uuid.UUID, user: User, reason: str, note: str = ""
) -> ProposedAction:
    if reason not in REJECTION_REASONS:
        raise ValidationFailedError(f"reason must be one of {sorted(REJECTION_REASONS)}")
    with transaction.atomic():
        action = (
            ProposedAction.objects.select_for_update()
            .filter(pk=action_id, workspace_id=workspace_id)
            .first()
        )
        if action is None:
            raise NotFoundError("action not found")
        _authorise(user, action)
        _move(action, ActionStatus.REJECTED)
        action.decided_by = user
        action.decided_at = timezone.now()
        action.decision_reason = reason
        action.decision_note = note
        action.save()
        record_event(
            action="action.rejected",
            actor=Actor.human(user.pk),
            workspace_id=workspace_id,
            target_type="action",
            target_id=action.pk,
            after={"reason": reason, "note": note[:200]},
        )
        events.publish(events.EventTopic.ACTION_DECIDED, _decision_payload(action, "rejected"))
    return action


def _decision_payload(action: ProposedAction, verdict: str) -> dict[str, Any]:
    return {
        "action_id": str(action.pk),
        "workspace_id": str(action.workspace_id),
        "kind": action.kind,
        "agent": action.requested_by,
        "verdict": verdict,
        "reason": action.decision_reason,
        "note": action.decision_note,
        "proposed": action.payload,
        "final": action.edited_payload or action.payload,
    }


def execute(action_id: uuid.UUID) -> ProposedAction:
    """Run an approved action once. Safe to call repeatedly: only the first caller executes."""
    with transaction.atomic():
        action = ProposedAction.objects.select_for_update().get(pk=action_id)
        if action.status not in {ActionStatus.APPROVED, ActionStatus.FAILED_RETRYABLE}:
            return action
        spec = get_action_kind(action.kind)
        if guardrails.kill_switch_active(action.workspace_id):
            record_event(
                action="action.execution_paused",
                actor=Actor.system("executor"),
                workspace_id=action.workspace_id,
                target_type="action",
                target_id=action.pk,
            )
            raise ActionBlockedError("kill switch is active", code=ErrorCode.KILL_SWITCH)
        _move(action, ActionStatus.EXECUTING)
        action.attempts += 1
        action.save()
        attempt = ExecutionAttempt.objects.create(
            workspace_id=action.workspace_id, action=action, started_at=timezone.now()
        )

    try:
        result = spec.executor(action)
    except RetryableProviderError as exc:
        _finish_attempt(attempt, ok=False, error=exc)
        _settle(action, ActionStatus.FAILED_RETRYABLE, error=str(exc))
        raise
    except Exception as exc:
        _finish_attempt(attempt, ok=False, error=exc)
        _settle(action, ActionStatus.FAILED_FINAL, error=f"{type(exc).__name__}: {exc}")
        log.exception("action_failed", action=str(action.pk), kind=action.kind)
        return action

    _finish_attempt(attempt, ok=True)
    _settle(action, ActionStatus.SUCCEEDED, result=result)
    if spec.verify is not None:
        verified = _safe_verify(spec, action)
        if verified:
            _settle(action, ActionStatus.VERIFIED)
        else:
            record_event(
                action="action.verification_failed",
                actor=Actor.system("executor"),
                workspace_id=action.workspace_id,
                target_type="action",
                target_id=action.pk,
            )
    if action.rollback_of_id:
        original = ProposedAction.objects.get(pk=action.rollback_of_id)
        _settle(original, ActionStatus.ROLLED_BACK)
    return action


def _safe_verify(spec: ActionKind, action: ProposedAction) -> bool:
    try:
        return bool(spec.verify(action)) if spec.verify else True
    except Exception:
        log.exception("verification_crashed", action=str(action.pk))
        return False


def _finish_attempt(attempt: ExecutionAttempt, *, ok: bool, error: Exception | None = None) -> None:
    attempt.finished_at = timezone.now()
    attempt.ok = ok
    if error is not None:
        attempt.error_class = type(error).__name__
        attempt.detail = {"message": str(error)[:500]}
    attempt.save()


def _settle(
    action: ProposedAction,
    status: ActionStatus,
    *,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    with transaction.atomic():
        fresh = ProposedAction.objects.select_for_update().get(pk=action.pk)
        _move(fresh, status)
        if result is not None:
            fresh.result = result
            fresh.executed_at = timezone.now()
        if error:
            fresh.error = error[:2000]
        fresh.save()
        record_event(
            action=f"action.{status}",
            actor=Actor.system("executor"),
            workspace_id=fresh.workspace_id,
            target_type="action",
            target_id=fresh.pk,
            after={"status": str(status), "error": error[:200]},
        )
        if status in {ActionStatus.SUCCEEDED, ActionStatus.FAILED_FINAL}:
            events.publish(
                events.EventTopic.ACTION_EXECUTED,
                {
                    "action_id": str(fresh.pk),
                    "workspace_id": str(fresh.workspace_id),
                    "status": str(status),
                },
            )
    action.status, action.result, action.error = fresh.status, fresh.result, fresh.error
    action.executed_at = fresh.executed_at


def fail_final(action_id: uuid.UUID, error: str) -> None:
    action = ProposedAction.objects.get(pk=action_id)
    if action.status == ActionStatus.FAILED_RETRYABLE:
        _settle(action, ActionStatus.FAILED_FINAL, error=error)


def request_rollback(
    *, workspace_id: uuid.UUID, action_id: uuid.UUID, user: User
) -> ProposedAction:
    action = get_action(workspace_id, action_id)
    spec = get_action_kind(action.kind)
    if spec.rollback_kind is None or spec.rollback_payload is None:
        raise ActionStateError("this action cannot be rolled back")
    if action.status not in {ActionStatus.SUCCEEDED, ActionStatus.VERIFIED}:
        raise ActionStateError("only executed actions can be rolled back")
    return propose(
        workspace_id=workspace_id,
        kind=spec.rollback_kind,
        payload=spec.rollback_payload(action),
        requested_by=f"user:{user.pk}",
        summary=f"Roll back: {action.summary}"[:300],
        idempotency_key=f"rollback-{action.pk}",
        rollback_of=action,
    )


def expire_stale() -> int:
    return ProposedAction.objects.filter(
        status=ActionStatus.AWAITING_APPROVAL, expires_at__lt=timezone.now()
    ).update(status=ActionStatus.EXPIRED)


def recover_stuck() -> int:
    """Executions that never reported back (worker died) become retryable again."""
    cutoff = timezone.now() - STUCK_AFTER
    return ProposedAction.objects.filter(
        Q(status=ActionStatus.EXECUTING), updated_at__lt=cutoff
    ).update(status=ActionStatus.FAILED_RETRYABLE)


REDISPATCH_AFTER = timedelta(minutes=2)


def redispatch_approved() -> int:
    """Approved actions that never started (the task was lost or hit an old worker) run again."""
    from actions.tasks import execute_action_task

    cutoff = timezone.now() - REDISPATCH_AFTER
    waiting = ProposedAction.objects.filter(
        status=ActionStatus.APPROVED,
        attempts=0,
        decided_at__lt=cutoff,
        workspace__writes_paused=False,
    )
    count = 0
    for action in waiting:
        execute_action_task.delay(str(action.pk))
        count += 1
    return count


def set_kill_switch(*, workspace_id: uuid.UUID, enabled: bool, user: User) -> None:
    from workspaces.models import Workspace

    require_permission(user, workspace_id, Permission.USE_KILL_SWITCH)
    Workspace.objects.filter(pk=workspace_id).update(writes_paused=enabled)
    record_event(
        action="kill_switch.enabled" if enabled else "kill_switch.disabled",
        actor=Actor.human(user.pk),
        workspace_id=workspace_id,
        target_type="workspace",
        target_id=workspace_id,
    )
