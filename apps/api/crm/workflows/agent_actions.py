"""The only door agents have into workflows: proposed actions that a person approves.

Enrolling someone can start messages, so it is R2 and always reaches the approvals inbox first.
Taking someone out is R1: it only stops things, and it is the rollback of the enrolment.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel

from actions.models import ProposedAction, RiskClass
from actions.registry import ActionKind, register_action_kind
from crm.workflows import engine
from crm.workflows.models import OPEN_STATUSES, Enrollment, Workflow

PREFIX = "crm.workflow."
ENROLL = PREFIX + "enroll"
EXIT = PREFIX + "exit"
ROLLBACK_REASON = "agent_rollback"


class EnrollContact(BaseModel):
    workflow_id: uuid.UUID
    contact_id: uuid.UUID


class ExitContact(BaseModel):
    workflow_id: uuid.UUID
    contact_id: uuid.UUID


def _ids(action: ProposedAction) -> tuple[uuid.UUID, uuid.UUID]:
    payload = action.effective_payload
    return uuid.UUID(str(payload["workflow_id"])), uuid.UUID(str(payload["contact_id"]))


def _enroll(action: ProposedAction) -> dict[str, Any]:
    workflow_id, contact_id = _ids(action)
    row = engine.enroll(action.workspace_id, workflow_id, contact_id, source="agent")
    return {"enrolled": row is not None, "enrollment_id": str(row.pk) if row else None}


def _exit(action: ProposedAction) -> dict[str, Any]:
    workflow_id, contact_id = _ids(action)
    rows = Enrollment.objects.filter(
        workspace_id=action.workspace_id,
        workflow_id=workflow_id,
        contact_id=contact_id,
        status__in=OPEN_STATUSES,
    )
    count = 0
    for row in rows:
        engine.exit_enrollment(row, ROLLBACK_REASON)
        count += 1
    return {"exited": count}


def _preview(action: ProposedAction) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    workflow_id, _ = _ids(action)
    workflow = Workflow.objects.filter(pk=workflow_id, workspace_id=action.workspace_id).first()
    return None, {"workflow": workflow.name if workflow else "", "status": "enrolled"}


def _rollback(action: ProposedAction) -> dict[str, Any]:
    workflow_id, contact_id = _ids(action)
    return {"workflow_id": str(workflow_id), "contact_id": str(contact_id)}


def register_action_kinds() -> None:
    register_action_kind(
        ActionKind(
            kind=ENROLL,
            payload_model=EnrollContact,
            risk=RiskClass.R2,
            executor=_enroll,
            description="Put a contact into a workflow. The workflow may then message them.",
            preview=_preview,
            rollback_kind=EXIT,
            rollback_payload=_rollback,
        )
    )
    register_action_kind(
        ActionKind(
            kind=EXIT,
            payload_model=ExitContact,
            risk=RiskClass.R1,
            executor=_exit,
            description="Take a contact out of a workflow. Nothing further is sent.",
        )
    )
