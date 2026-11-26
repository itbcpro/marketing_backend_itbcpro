"""Guardrails: rules that hold independently of what any agent wants.

A rule returns violations; any violation blocks the action before a human even sees it.
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db.models import Sum
from django.utils import timezone

from actions.models import ActionStatus, Guardrail, ProposedAction
from actions.registry import ActionKind
from core import flags
from workspaces.models import Workspace


@dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "detail": self.detail}


Rule = Callable[[ProposedAction, ActionKind], list[Violation]]
_rules: list[Rule] = []


def register_rule(rule: Rule) -> Rule:
    _rules.append(rule)
    return rule


def kill_switch_active(workspace_id: uuid.UUID) -> bool:
    if flags.is_enabled("global_kill_switch"):
        return True
    return Workspace.objects.filter(pk=workspace_id, writes_paused=True).exists()


def _config(action: ProposedAction, key: str) -> dict[str, Any] | None:
    guardrail = Guardrail.objects.filter(workspace=action.workspace, key=key, enabled=True).first()
    return guardrail.config if guardrail else None


@register_rule
def kill_switch_rule(action: ProposedAction, kind: ActionKind) -> list[Violation]:
    if kill_switch_active(action.workspace_id):
        return [Violation("kill_switch", "external changes are paused for this workspace")]
    return []


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


@register_rule
def blocked_terms_rule(action: ProposedAction, kind: ActionKind) -> list[Violation]:
    config = _config(action, "blocked_terms")
    if not config:
        return []
    text = " ".join(_strings(action.payload)).lower()
    hits = sorted({t for t in config.get("terms", []) if t and t.lower() in text})
    return [Violation("blocked_terms", f"contains blocked term: {t}") for t in hits]


@register_rule
def daily_spend_cap_rule(action: ProposedAction, kind: ActionKind) -> list[Violation]:
    config = _config(action, "daily_spend_cap_micros")
    if not config or action.spend_micros <= 0:
        return []
    since = timezone.now() - timedelta(hours=24)
    committed = (
        ProposedAction.objects.filter(
            workspace=action.workspace,
            created_at__gte=since,
            status__in=[
                ActionStatus.APPROVED,
                ActionStatus.EXECUTING,
                ActionStatus.SUCCEEDED,
                ActionStatus.VERIFIED,
            ],
        )
        .exclude(pk=action.pk)
        .aggregate(total=Sum("spend_micros"))["total"]
        or 0
    )
    cap = int(config.get("amount", 0))
    if cap and committed + action.spend_micros > cap:
        return [
            Violation(
                "daily_spend_cap",
                f"would raise 24h committed spend to {committed + action.spend_micros} "
                f"micros, cap is {cap}",
            )
        ]
    return []


@register_rule
def max_single_spend_rule(action: ProposedAction, kind: ActionKind) -> list[Violation]:
    config = _config(action, "max_single_spend_micros")
    cap = int(config.get("amount", 0)) if config else 0
    if cap and action.spend_micros > cap:
        return [Violation("max_single_spend", f"spend {action.spend_micros} exceeds {cap} micros")]
    return []


def evaluate(action: ProposedAction, kind: ActionKind) -> list[Violation]:
    violations: list[Violation] = []
    for rule in _rules:
        violations.extend(rule(action, kind))
    return violations


def payload_fingerprint(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
