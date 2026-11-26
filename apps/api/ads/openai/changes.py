"""Turn a wanted change into a proposed action, applied at once only when the user may approve."""

import time
from typing import Any

from actions import services as action_services
from actions.models import ActionStatus, ProposedAction
from ads import providers
from connections.models import Connection
from core.errors import PermissionDeniedError
from identity.models import User

DONE = {ActionStatus.VERIFIED, ActionStatus.SUCCEEDED}
FAILED = {
    ActionStatus.FAILED_FINAL,
    ActionStatus.REJECTED,
    ActionStatus.EXPIRED,
    ActionStatus.POLICY_BLOCKED,
    ActionStatus.ROLLED_BACK,
}
POLL_SECONDS = 0.5
DEFAULT_WAIT = 45.0


def wait_for(action: ProposedAction, timeout: float = DEFAULT_WAIT) -> ProposedAction:
    """Approved actions run in the background; wait until this one is finished (or gave up)."""
    deadline = time.monotonic() + timeout
    while action.status not in DONE | FAILED | {ActionStatus.AWAITING_APPROVAL}:
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
        action.refresh_from_db()
    return action


def submit(
    connection: Connection,
    user: User,
    kind_suffix: str,
    payload: dict[str, Any],
    *,
    summary: str = "",
    apply_now: bool = False,
    idempotency_key: str | None = None,
    wait: bool = False,
) -> tuple[ProposedAction, bool]:
    """Propose the change; with ``apply_now`` also approve it when the user is allowed to."""
    action = action_services.propose(
        workspace_id=connection.workspace_id,
        kind=providers.for_connection(connection).action_prefix + kind_suffix,
        payload={**payload, "connection_id": str(connection.pk)},
        requested_by=f"user:{user.pk}",
        summary=summary,
        idempotency_key=idempotency_key,
    )
    applied = False
    if apply_now and action.status == "awaiting_approval":
        try:
            action = action_services.approve(
                workspace_id=connection.workspace_id, action_id=action.pk, user=user
            )
            applied = True
        except PermissionDeniedError:
            pass  # the change stays in the approvals inbox for someone who can decide
    if wait and action.status not in {ActionStatus.AWAITING_APPROVAL, *DONE, *FAILED}:
        action = wait_for(action)
    return action, applied
