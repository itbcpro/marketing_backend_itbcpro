"""Autonomy: when an action may skip human approval.

Rules, in order: a run that read untrusted content ("tainted") never auto-approves; R3 never
auto-approves; R1 needs level 3; R2 needs level 3 with a spend limit that covers the action;
level 4 relaxes only the R2 spend-limit requirement. Absence of a grant means level 2.
"""

from django.utils import timezone

from actions.models import AutonomyGrant, ProposedAction, RiskClass

LEVEL_SUGGEST = 1
LEVEL_PREPARE = 2
LEVEL_ACT = 3
LEVEL_FULL = 4


def level_for(action: ProposedAction) -> int:
    grant = AutonomyGrant.objects.filter(
        workspace=action.workspace, agent=action.requested_by, action_kind=action.kind
    ).first()
    if grant is None or (grant.expires_at and grant.expires_at <= timezone.now()):
        return LEVEL_PREPARE
    return grant.level


def may_auto_approve(action: ProposedAction) -> bool:
    if action.tainted or action.risk == RiskClass.R3:
        return False
    level = level_for(action)
    if level < LEVEL_ACT:
        return False
    if action.risk == RiskClass.R1:
        return True
    if action.risk == RiskClass.R2:
        grant = AutonomyGrant.objects.get(
            workspace=action.workspace, agent=action.requested_by, action_kind=action.kind
        )
        limit = int(grant.limits.get("max_spend_micros", 0))
        return level == LEVEL_FULL or (limit > 0 and action.spend_micros <= limit)
    return False
