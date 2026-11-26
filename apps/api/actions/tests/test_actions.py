from datetime import timedelta

import pytest
from django.utils import timezone
from pydantic import BaseModel

from actions import services
from actions.models import ActionStatus, AutonomyGrant, Guardrail, RiskClass
from actions.registry import (
    ActionKind,
    register_action_kind,
    unregister_action_kind,
)
from conftest import make_user
from core.errors import (
    NotFoundError,
    PermissionDeniedError,
    RetryableProviderError,
    ValidationFailedError,
)
from workspaces.models import Role, Workspace

pytestmark = pytest.mark.django_db


class Payload(BaseModel):
    text: str
    spend_micros: int = 0


class Env:
    """Records executor calls so tests can assert what really ran."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_with: Exception | None = None
        self.verify_ok = True

    def executor(self, action) -> dict:
        self.calls.append(dict(action.effective_payload))
        if self.fail_with:
            raise self.fail_with
        return {"done": True}


@pytest.fixture
def env():
    env = Env()
    for name, risk in (("t.r1", RiskClass.R1), ("t.r2", RiskClass.R2), ("t.r3", RiskClass.R3)):
        register_action_kind(
            ActionKind(
                kind=name,
                payload_model=Payload,
                risk=risk,
                executor=env.executor,
                spend_micros=lambda p: p.spend_micros,
                verify=lambda a: env.verify_ok,
                preview=lambda a: ({"state": "before"}, {"state": "after"}),
                rollback_kind="t.undo" if name == "t.r2" else None,
                rollback_payload=(lambda a: {"text": f"undo {a.pk}"}) if name == "t.r2" else None,
            )
        )
    register_action_kind(
        ActionKind(kind="t.undo", payload_model=Payload, risk=RiskClass.R1, executor=env.executor)
    )
    yield env
    for name in ("t.r1", "t.r2", "t.r3", "t.undo"):
        unregister_action_kind(name)


def _propose(workspace: Workspace, kind: str = "t.r1", **kw):
    payload = kw.pop("payload", {"text": "hello"})
    return services.propose(
        workspace_id=workspace.pk,
        kind=kind,
        payload=payload,
        requested_by=kw.pop("agent", "ads"),
        **kw,
    )


def _approver(workspace: Workspace, role: Role = Role.APPROVER):
    from identity.models import User

    email = f"{role}-{workspace.pk}@example.com"
    return User.objects.filter(email=email).first() or make_user(email, workspace, role)


def _approve(workspace, action, user, capture, **kw):
    with capture(execute=True):
        result = services.approve(workspace_id=workspace.pk, action_id=action.pk, user=user, **kw)
    result.refresh_from_db()
    return result


# ---- lifecycle --------------------------------------------------------------------------------


def test_proposal_waits_for_a_human_then_executes_once(
    workspace, env, django_capture_on_commit_callbacks
) -> None:
    action = _propose(workspace, summary="Say hello")
    assert action.status == ActionStatus.AWAITING_APPROVAL and action.before == {"state": "before"}
    assert env.calls == []

    done = _approve(
        workspace, action, _approver(workspace), django_capture_on_commit_callbacks, note="ok"
    )
    done.refresh_from_db()
    assert done.status == ActionStatus.VERIFIED and env.calls == [
        {"text": "hello", "spend_micros": 0}
    ]
    assert done.attempts == 1 and done.attempt_log.get().ok
    assert (
        services.execute(done.pk).status == ActionStatus.VERIFIED and len(env.calls) == 1
    )  # no second run


def test_proposals_are_idempotent(workspace, env) -> None:
    first, second = _propose(workspace), _propose(workspace)
    assert first.pk == second.pk
    assert (
        _propose(workspace, idempotency_key="k").pk == _propose(workspace, idempotency_key="k").pk
    )


def test_invalid_payloads_are_rejected_before_anything_is_stored(workspace, env) -> None:
    with pytest.raises(ValidationFailedError):
        _propose(workspace, payload={"spend_micros": "many"})


def test_human_edits_are_what_executes(workspace, env, django_capture_on_commit_callbacks) -> None:
    action = _propose(workspace)
    done = _approve(
        workspace,
        action,
        _approver(workspace),
        django_capture_on_commit_callbacks,
        edits={"text": "edited"},
    )
    assert env.calls == [{"text": "edited", "spend_micros": 0}]
    assert done.payload["text"] == "hello" and done.edited_payload["text"] == "edited"
    with pytest.raises(ValidationFailedError):
        services.approve(
            workspace_id=workspace.pk,
            action_id=_propose(workspace, payload={"text": "b"}).pk,
            user=_approver(workspace),
            edits={"nope": 1},
        )


def test_rejection_needs_a_known_reason_and_is_final(workspace, env) -> None:
    action, user = _propose(workspace), _approver(workspace)
    with pytest.raises(ValidationFailedError):
        services.reject(workspace_id=workspace.pk, action_id=action.pk, user=user, reason="because")
    rejected = services.reject(
        workspace_id=workspace.pk, action_id=action.pk, user=user, reason="tone", note="too pushy"
    )
    assert rejected.status == ActionStatus.REJECTED and rejected.decision_reason == "tone"
    with pytest.raises(services.ActionStateError):
        services.approve(workspace_id=workspace.pk, action_id=action.pk, user=user)


def test_every_status_has_a_transition_entry_and_terminals_are_closed() -> None:
    assert set(services.TRANSITIONS) == set(ActionStatus)
    for terminal in (
        ActionStatus.REJECTED,
        ActionStatus.EXPIRED,
        ActionStatus.POLICY_BLOCKED,
        ActionStatus.FAILED_FINAL,
        ActionStatus.ROLLED_BACK,
    ):
        assert services.TRANSITIONS[terminal] == frozenset()
    reachable = {t for targets in services.TRANSITIONS.values() for t in targets}
    assert reachable | {ActionStatus.AWAITING_APPROVAL} >= set(ActionStatus)


# ---- authorisation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "role", "allowed"),
    [
        ("t.r1", Role.APPROVER, True),
        ("t.r1", Role.EDITOR, False),
        ("t.r1", Role.VIEWER, False),
        ("t.r2", Role.APPROVER, True),
        ("t.r2", Role.ANALYST, False),
        ("t.r3", Role.APPROVER, False),
        ("t.r3", Role.ADMIN, True),
        ("t.r3", Role.OWNER, True),
    ],
)
def test_approval_permission_depends_on_risk_class(
    workspace, env, kind, role, allowed, django_capture_on_commit_callbacks
) -> None:
    action = _propose(workspace, kind)
    user = make_user(f"u-{kind}-{role}@example.com", workspace, role)
    if allowed:
        assert _approve(workspace, action, user, django_capture_on_commit_callbacks).status in {
            ActionStatus.VERIFIED,
            ActionStatus.SUCCEEDED,
        }
    else:
        with pytest.raises(PermissionDeniedError):
            services.approve(workspace_id=workspace.pk, action_id=action.pk, user=user)


def test_actions_are_invisible_across_workspaces(workspace, env, owner) -> None:
    from workspaces.services import create_workspace

    other = create_workspace(name="Other", owner=owner)
    action = _propose(workspace)
    with pytest.raises(NotFoundError):
        services.get_action(other.pk, action.pk)
    with pytest.raises(NotFoundError):
        services.approve(workspace_id=other.pk, action_id=action.pk, user=owner)


# ---- guardrails -------------------------------------------------------------------------------


def test_blocked_terms_stop_an_action_before_review(workspace, env) -> None:
    Guardrail.objects.create(
        workspace=workspace, key="blocked_terms", config={"terms": ["guaranteed results"]}
    )
    blocked = _propose(workspace, payload={"text": "Get GUARANTEED results today"})
    assert blocked.status == ActionStatus.POLICY_BLOCKED
    assert blocked.violations[0]["rule"] == "blocked_terms"
    with pytest.raises(services.ActionBlockedError, match="blocked_terms"):
        services.approve(workspace_id=workspace.pk, action_id=blocked.pk, user=_approver(workspace))


def test_spend_caps_apply_per_action_and_per_day(
    workspace, env, django_capture_on_commit_callbacks
) -> None:
    Guardrail.objects.create(
        workspace=workspace, key="max_single_spend_micros", config={"amount": 50_000_000}
    )
    Guardrail.objects.create(
        workspace=workspace, key="daily_spend_cap_micros", config={"amount": 80_000_000}
    )
    too_big = _propose(workspace, payload={"text": "a", "spend_micros": 60_000_000})
    assert too_big.violations[0]["rule"] == "max_single_spend"

    first = _propose(workspace, payload={"text": "b", "spend_micros": 50_000_000})
    _approve(workspace, first, _approver(workspace), django_capture_on_commit_callbacks)
    second = _propose(workspace, payload={"text": "c", "spend_micros": 40_000_000})
    assert (
        second.status == ActionStatus.POLICY_BLOCKED
        and second.violations[0]["rule"] == "daily_spend_cap"
    )


def test_kill_switch_blocks_new_and_pending_actions(
    workspace, env, owner, django_capture_on_commit_callbacks
) -> None:
    pending = _propose(workspace)
    services.set_kill_switch(workspace_id=workspace.pk, enabled=True, user=owner)
    assert _propose(workspace, payload={"text": "new"}).status == ActionStatus.POLICY_BLOCKED
    with pytest.raises(services.ActionBlockedError):
        services.approve(workspace_id=workspace.pk, action_id=pending.pk, user=_approver(workspace))
    assert env.calls == []

    services.set_kill_switch(workspace_id=workspace.pk, enabled=False, user=owner)
    assert (
        _approve(
            workspace, pending, _approver(workspace), django_capture_on_commit_callbacks
        ).status
        == ActionStatus.VERIFIED
    )


def test_kill_switch_pauses_an_action_approved_earlier(
    workspace, env, owner, django_capture_on_commit_callbacks
) -> None:
    action = _propose(workspace)
    with django_capture_on_commit_callbacks(execute=False):
        services.approve(workspace_id=workspace.pk, action_id=action.pk, user=_approver(workspace))
    services.set_kill_switch(workspace_id=workspace.pk, enabled=True, user=owner)
    with pytest.raises(services.ActionBlockedError):
        services.execute(action.pk)
    action.refresh_from_db()
    assert action.status == ActionStatus.APPROVED and env.calls == []


def test_only_kill_switch_holders_can_flip_it(workspace, env) -> None:
    with pytest.raises(PermissionDeniedError):
        services.set_kill_switch(
            workspace_id=workspace.pk,
            enabled=True,
            user=make_user("v@example.com", workspace, Role.VIEWER),
        )


# ---- autonomy ---------------------------------------------------------------------------------


def _grant(workspace, kind, level, **limits):
    return AutonomyGrant.objects.create(
        workspace=workspace, agent="ads", action_kind=kind, level=level, limits=limits
    )


def test_level_three_runs_low_risk_actions_without_a_human(
    workspace, env, django_capture_on_commit_callbacks
) -> None:
    _grant(workspace, "t.r1", 3)
    with django_capture_on_commit_callbacks(execute=True):
        action = _propose(workspace)
    action.refresh_from_db()
    assert action.auto_approved and action.status == ActionStatus.VERIFIED and len(env.calls) == 1


def test_default_level_never_acts_alone(workspace, env, django_capture_on_commit_callbacks) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        action = _propose(workspace)
    assert action.status == ActionStatus.AWAITING_APPROVAL and env.calls == []


def test_tainted_runs_and_r3_actions_never_auto_approve(
    workspace, env, django_capture_on_commit_callbacks
) -> None:
    _grant(workspace, "t.r1", 4)
    _grant(workspace, "t.r3", 4)
    with django_capture_on_commit_callbacks(execute=True):
        tainted = _propose(workspace, tainted=True)
        r3 = _propose(workspace, "t.r3")
    assert (
        tainted.status == ActionStatus.AWAITING_APPROVAL
        and r3.status == ActionStatus.AWAITING_APPROVAL
    )
    assert env.calls == []


def test_r2_auto_approval_needs_a_covering_spend_limit(
    workspace, env, django_capture_on_commit_callbacks
) -> None:
    _grant(workspace, "t.r2", 3, max_spend_micros=10_000_000)
    with django_capture_on_commit_callbacks(execute=True):
        small = _propose(workspace, "t.r2", payload={"text": "s", "spend_micros": 5_000_000})
        large = _propose(workspace, "t.r2", payload={"text": "l", "spend_micros": 20_000_000})
    assert small.auto_approved and large.status == ActionStatus.AWAITING_APPROVAL


def test_expired_grants_fall_back_to_human_approval(workspace, env) -> None:
    grant = _grant(workspace, "t.r1", 3)
    AutonomyGrant.objects.filter(pk=grant.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    assert _propose(workspace).status == ActionStatus.AWAITING_APPROVAL


# ---- failures, recovery, rollback -------------------------------------------------------------


def test_retryable_failures_can_be_retried_then_succeed(
    workspace, env, django_capture_on_commit_callbacks
) -> None:
    env.fail_with = RetryableProviderError("busy", provider="x")
    action = _propose(workspace)
    with django_capture_on_commit_callbacks(execute=False):
        services.approve(workspace_id=workspace.pk, action_id=action.pk, user=_approver(workspace))
    with pytest.raises(RetryableProviderError):
        services.execute(action.pk)
    action.refresh_from_db()
    assert action.status == ActionStatus.FAILED_RETRYABLE

    env.fail_with = None
    assert services.execute(action.pk).status == ActionStatus.VERIFIED
    assert action.attempt_log.count() == 2


def test_permanent_failures_are_final_and_visible(
    workspace, env, django_capture_on_commit_callbacks
) -> None:
    env.fail_with = ValueError("bad payload")
    action = _propose(workspace)
    done = _approve(workspace, action, _approver(workspace), django_capture_on_commit_callbacks)
    assert done.status == ActionStatus.FAILED_FINAL and "bad payload" in done.error
    assert services.execute(action.pk).status == ActionStatus.FAILED_FINAL and len(env.calls) == 1


def test_failed_verification_leaves_the_action_succeeded_not_verified(
    workspace, env, django_capture_on_commit_callbacks
) -> None:
    env.verify_ok = False
    action = _propose(workspace)
    assert (
        _approve(workspace, action, _approver(workspace), django_capture_on_commit_callbacks).status
        == ActionStatus.SUCCEEDED
    )


def test_rollback_flows_through_the_same_layer(
    workspace, env, owner, django_capture_on_commit_callbacks
) -> None:
    action = _propose(workspace, "t.r2")
    _approve(workspace, action, _approver(workspace), django_capture_on_commit_callbacks)
    undo = services.request_rollback(workspace_id=workspace.pk, action_id=action.pk, user=owner)
    assert (
        undo.kind == "t.undo"
        and undo.rollback_of_id == action.pk
        and undo.status == ActionStatus.AWAITING_APPROVAL
    )
    _approve(workspace, undo, _approver(workspace), django_capture_on_commit_callbacks)
    action.refresh_from_db()
    assert action.status == ActionStatus.ROLLED_BACK
    with pytest.raises(services.ActionStateError):
        services.request_rollback(
            workspace_id=workspace.pk,
            action_id=_propose(workspace, "t.r1", payload={"text": "x"}).pk,
            user=owner,
        )


def test_stale_proposals_expire_and_stuck_executions_recover(workspace, env) -> None:
    from actions.models import ProposedAction

    old = _propose(workspace)
    ProposedAction.objects.filter(pk=old.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    assert services.expire_stale() == 1
    old.refresh_from_db()
    assert old.status == ActionStatus.EXPIRED

    stuck = _propose(workspace, payload={"text": "stuck"})
    ProposedAction.objects.filter(pk=stuck.pk).update(
        status=ActionStatus.EXECUTING, updated_at=timezone.now() - timedelta(hours=1)
    )
    assert services.recover_stuck() == 1
    stuck.refresh_from_db()
    assert stuck.status == ActionStatus.FAILED_RETRYABLE


def test_decisions_are_published_for_learning(workspace, env) -> None:
    from audit import outbox

    seen: list[dict] = []
    outbox.register_handler("action.decided", seen.append)
    action = _propose(workspace)
    services.reject(
        workspace_id=workspace.pk, action_id=action.pk, user=_approver(workspace), reason="brand"
    )
    outbox.dispatch_batch()
    assert any(
        e["verdict"] == "rejected" and e["reason"] == "brand" and e["agent"] == "ads" for e in seen
    )


def test_approved_actions_that_never_started_are_dispatched_again(workspace, env) -> None:
    from actions.models import ProposedAction

    fresh = _propose(workspace, payload={"text": "fresh"})
    lost = _propose(workspace, payload={"text": "lost"})
    for action, age in ((fresh, 0), (lost, 5)):
        ProposedAction.objects.filter(pk=action.pk).update(
            status=ActionStatus.APPROVED, decided_at=timezone.now() - timedelta(minutes=age)
        )
    assert services.redispatch_approved() == 1
    lost.refresh_from_db()
    fresh.refresh_from_db()
    assert lost.status in {ActionStatus.SUCCEEDED, ActionStatus.VERIFIED}
    assert fresh.status == ActionStatus.APPROVED
    assert [c["text"] for c in env.calls] == ["lost"]


def test_paused_workspaces_are_not_redispatched(workspace, env) -> None:
    from actions.models import ProposedAction
    from workspaces.models import Workspace

    action = _propose(workspace, payload={"text": "paused"})
    ProposedAction.objects.filter(pk=action.pk).update(
        status=ActionStatus.APPROVED, decided_at=timezone.now() - timedelta(minutes=10)
    )
    Workspace.objects.filter(pk=workspace.pk).update(writes_paused=True)
    assert services.redispatch_approved() == 0
    assert env.calls == []


def test_a_worker_that_does_not_know_the_action_kind_waits_instead_of_losing_it(workspace) -> None:
    from celery.exceptions import Retry

    from actions.models import ProposedAction
    from actions.tasks import execute_action_task

    action = ProposedAction.objects.create(
        workspace_id=workspace.pk,
        kind="t.from_the_future",
        payload={},
        risk="r1",
        status=ActionStatus.APPROVED,
        requested_by="test",
        idempotency_key="future-kind",
    )
    with pytest.raises(Retry):
        execute_action_task.apply(args=[str(action.pk)], throw=True)
    action.refresh_from_db()
    assert action.status == ActionStatus.APPROVED
