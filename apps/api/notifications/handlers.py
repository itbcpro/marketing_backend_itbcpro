"""Reactions to domain events: tell approvers what is waiting and stream live updates."""

from typing import Any

from actions.models import ActionStatus, ProposedAction, RiskClass
from core import sse
from core.logging import get_logger
from notifications.email import send_templated_email
from workspaces.models import Membership, Role

log = get_logger(__name__)

APPROVER_ROLES = {
    RiskClass.R1: {Role.OWNER, Role.ADMIN, Role.APPROVER},
    RiskClass.R2: {Role.OWNER, Role.ADMIN, Role.APPROVER},
    RiskClass.R3: {Role.OWNER, Role.ADMIN},
}


def on_action_proposed(payload: dict[str, Any]) -> None:
    """Outbox handler for ``action.proposed``. Idempotent enough: a retry may resend one email."""
    action = (
        ProposedAction.objects.select_related("workspace").filter(pk=payload["action_id"]).first()
    )
    if action is None:
        return
    sse.publish(
        sse.workspace_channel(action.workspace_id),
        "action.updated",
        {"action_id": str(action.pk), "status": action.status, "kind": action.kind},
    )
    if action.status != ActionStatus.AWAITING_APPROVAL:
        return
    roles = APPROVER_ROLES.get(RiskClass(action.risk), {Role.OWNER})
    recipients = Membership.objects.filter(
        workspace=action.workspace, role__in=roles
    ).select_related("user")
    for membership in recipients:
        try:
            send_templated_email(
                membership.user.email,
                "approval_needed",
                {
                    "workspace": action.workspace.name,
                    "agent": action.requested_by,
                    "summary": action.summary or action.kind,
                    "risk": action.risk.upper(),
                },
            )
        except Exception:
            log.exception("approval_email_failed", action=str(action.pk))
