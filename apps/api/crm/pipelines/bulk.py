"""Bulk actions on deals.

Each action is an entry of ``ACTIONS`` with what it needs and what permission it takes; a new action
is a new entry and nothing else changes. A deal an action cannot be applied to (closed, other
pipeline, missing requirement) is skipped and counted, never a reason to stop the rest.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings

from audit.actors import Actor
from core.errors import ConflictError, ValidationFailedError
from crm.pipelines import services
from crm.pipelines.models import Opportunity
from workspaces.models import Membership
from workspaces.permissions import Permission


@dataclass(frozen=True)
class Params:
    stage_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    lost_reason_id: uuid.UUID | None = None
    priority: str | None = None
    tag: str | None = None


@dataclass(frozen=True)
class Result:
    done: int
    skipped: int


Run = Callable[[uuid.UUID, Actor, Opportunity, Params], object]


@dataclass(frozen=True)
class Action:
    run: Run
    requires: tuple[str, ...] = ()
    permission: Permission = Permission.CRM_EDIT


def _required[T](value: T | None) -> T:
    """The parameter an action declared in ``requires``; ``apply`` has already checked it."""
    if value is None:
        raise ValidationFailedError("missing_parameter")
    return value


def _move(ws: uuid.UUID, actor: Actor, deal: Opportunity, p: Params) -> object:
    return services.move_opportunity(ws, actor, deal, _required(p.stage_id))


def _add_tag(ws: uuid.UUID, actor: Actor, deal: Opportunity, p: Params) -> object:
    return services.update_opportunity(ws, actor, deal, tags=[*deal.tags, _required(p.tag)])


ACTIONS: dict[str, Action] = {
    "move": Action(_move, requires=("stage_id",)),
    "assign": Action(lambda ws, a, d, p: services.assign_opportunity(ws, a, d, p.user_id)),
    "win": Action(lambda ws, a, d, p: services.win(ws, a, d)),
    "lose": Action(
        lambda ws, a, d, p: services.lose(ws, a, d, lost_reason_id=p.lost_reason_id),
    ),
    "abandon": Action(lambda ws, a, d, p: services.abandon(ws, a, d)),
    "reopen": Action(lambda ws, a, d, p: services.reopen(ws, a, d)),
    "set_priority": Action(
        lambda ws, a, d, p: services.update_opportunity(ws, a, d, priority=p.priority),
        requires=("priority",),
    ),
    "add_tag": Action(_add_tag, requires=("tag",)),
    "delete": Action(
        lambda ws, a, d, p: services.delete_opportunity(ws, d), permission=Permission.CRM_DELETE
    ),
}


def action_named(name: str) -> Action:
    found = ACTIONS.get(name)
    if found is None:
        raise ValidationFailedError("unknown_action", errors={"action": "unknown_action"})
    return found


def apply(
    workspace_id: uuid.UUID,
    actor: Actor,
    deal_ids: list[uuid.UUID],
    name: str,
    params: Params,
    membership: Membership | None = None,
) -> Result:
    action = action_named(name)
    missing = [key for key in action.requires if getattr(params, key) is None]
    if missing:
        raise ValidationFailedError("missing_parameter", errors=dict.fromkeys(missing, "required"))
    if len(deal_ids) > settings.CRM_BULK_MAX_RECORDS:
        raise ValidationFailedError("too_many_records")
    done = 0
    wanted = set(deal_ids)
    deals = services.list_opportunities(workspace_id, membership).filter(pk__in=wanted)
    for deal in deals:
        try:
            action.run(workspace_id, actor, deal, params)
        except ConflictError, ValidationFailedError:
            continue
        done += 1
    return Result(done=done, skipped=len(wanted) - done)
