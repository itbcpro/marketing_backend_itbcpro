"""Plan limits (public surface of the module).

A limit comes from the organization's own overrides first, then from its plan. A limit that is
missing from both means the plan allows as much as needed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from core.errors import ErrorCode, NotFoundError, ValidationFailedError
from crm.plans.models import Plan
from workspaces.models import Organization, Workspace

MESSAGES = "messages_per_month"
CONTACTS = "contacts"
WORKFLOWS = "workflows"
LIMIT_KEYS = (MESSAGES, CONTACTS, WORKFLOWS)
DEFAULT_PLAN = "free"


class PlanLimitError(ValidationFailedError):
    """The workspace is at what its plan allows. The message says which limit."""

    code = ErrorCode.PLAN_LIMIT
    http_status = 402


def _organization(workspace_id: uuid.UUID) -> Organization:
    return Workspace.objects.select_related("organization").get(pk=workspace_id).organization


def plan_of(organization: Organization) -> Plan:
    found = Plan.objects.filter(key=organization.plan_key).first()
    if found is None:
        found = Plan.objects.filter(key=DEFAULT_PLAN).first()
    if found is None:
        raise NotFoundError("no plans are set up")
    return found


def limit_for(workspace_id: uuid.UUID, key: str) -> int | None:
    """What the workspace may use of ``key``, or ``None`` for no limit."""
    organization = _organization(workspace_id)
    override = organization.limits.get(key)
    if override is not None:
        return int(override)
    value = plan_of(organization).limits.get(key)
    return None if value is None else int(value)


def ensure_room(
    workspace_id: uuid.UUID, key: str, used: Callable[[], int], adding: int = 1
) -> None:
    """Refuse when ``adding`` more of ``key`` would pass the limit (``used`` is read only then)."""
    cap = limit_for(workspace_id, key)
    if cap is not None and used() + adding > cap:
        raise PlanLimitError("plan limit reached", errors={key: "plan_limit"})


def set_plan(organization: Organization, plan_key: str) -> Organization:
    if not Plan.objects.filter(key=plan_key).exists():
        raise ValidationFailedError("unknown plan", errors={"plan": "unknown"})
    organization.plan_key = plan_key
    organization.save(update_fields=["plan_key", "updated_at"])
    return organization


def summary(workspace_id: uuid.UUID, used: dict[str, int]) -> dict[str, Any]:
    organization = _organization(workspace_id)
    plan = plan_of(organization)
    return {
        "plan_key": plan.key,
        "plan_name": plan.name,
        "price_micros": plan.price_micros,
        "limits": [
            {"key": key, "limit": limit_for(workspace_id, key), "used": used.get(key, 0)}
            for key in LIMIT_KEYS
        ],
    }
