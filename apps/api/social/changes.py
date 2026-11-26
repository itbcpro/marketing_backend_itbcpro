"""Turn a wanted change into a proposed action, applied at once only when the user may approve."""

import uuid
from typing import Any

from actions import services as action_services
from actions.models import ActionStatus, ProposedAction
from core.errors import PermissionDeniedError
from identity.models import User
from social.response_settings import AGENT


def submit(
    workspace_id: uuid.UUID,
    user: User,
    kind: str,
    payload: dict[str, Any],
    *,
    summary: str = "",
    apply_now: bool = True,
) -> tuple[ProposedAction, bool]:
    """Propose the change as the user; with ``apply_now`` also approve it when they are allowed."""
    action = action_services.propose(
        workspace_id=workspace_id,
        kind=kind,
        payload=payload,
        requested_by=f"user:{user.pk}",
        summary=summary,
    )
    applied = False
    if apply_now and action.status == ActionStatus.AWAITING_APPROVAL:
        try:
            action = action_services.approve(
                workspace_id=workspace_id, action_id=action.pk, user=user
            )
            applied = True
        except PermissionDeniedError:
            pass  # stays in the approvals inbox for someone who can decide
    return action, applied


def propose_as_responder(
    workspace_id: uuid.UUID, kind: str, payload: dict[str, Any], *, summary: str = ""
) -> ProposedAction:
    """Propose as the community responder: executes alone only when the owner granted autonomy."""
    return action_services.propose(
        workspace_id=workspace_id,
        kind=kind,
        payload=payload,
        requested_by=AGENT,
        summary=summary,
    )


def approve_as(action: ProposedAction, user: User) -> ProposedAction:
    return action_services.approve(workspace_id=action.workspace_id, action_id=action.pk, user=user)
