"""Registry of action kinds. Adding a platform capability means registering a kind, nothing else."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from actions.models import ProposedAction, RiskClass

Executor = Callable[[ProposedAction], dict[str, Any]]
Previewer = Callable[[ProposedAction], tuple[dict[str, Any] | None, dict[str, Any] | None]]
Verifier = Callable[[ProposedAction], bool]
SpendOf = Callable[[BaseModel], int]
RollbackPlanner = Callable[[ProposedAction], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ActionKind:
    kind: str
    payload_model: type[BaseModel]
    risk: RiskClass
    executor: Executor
    description: str = ""
    preview: Previewer | None = None
    verify: Verifier | None = None
    spend_micros: SpendOf | None = None
    rollback_kind: str | None = None
    rollback_payload: RollbackPlanner | None = None


class UnknownActionKindError(KeyError):
    """Raised for an action kind nobody registered."""


_kinds: dict[str, ActionKind] = {}


def register_action_kind(kind: ActionKind) -> None:
    if kind.risk == RiskClass.R0:
        raise ValueError("R0 operations are reads and never become actions")
    if (kind.rollback_kind is None) != (kind.rollback_payload is None):
        raise ValueError("rollback_kind and rollback_payload must be given together")
    _kinds[kind.kind] = kind


def unregister_action_kind(name: str) -> None:
    _kinds.pop(name, None)


def get_action_kind(name: str) -> ActionKind:
    try:
        return _kinds[name]
    except KeyError as exc:
        raise UnknownActionKindError(name) from exc


def all_action_kinds() -> list[ActionKind]:
    return sorted(_kinds.values(), key=lambda k: k.kind)
