"""Test mode: walk a definition for one real contact without sending or changing anything.

Branches are decided from the contact's current data. Waits are skipped over, and a wait for a
reply is assumed to time out, so the walk always reaches an end.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from crm.contacts import services as contacts
from crm.workflows import actions
from crm.workflows.conditions import evaluate

WALK_LIMIT = 200


@dataclass(frozen=True)
class Visit:
    step_id: str
    step_type: str
    note: str


def _decide(action: actions.Action, config: Any, contact: dict[str, Any]) -> tuple[str, str]:
    """(what happens, where it goes instead of ``next``)"""
    if action.key == "branch":
        passed = evaluate(config.rules, config.match, contact)
        return ("yes" if passed else "no"), config.yes if passed else config.no
    if action.key == "wait_for_event":
        return "timeout", config.on_timeout
    if action.key == "finish":
        return "finish", ""
    return {"wait": "wait", "send_message": "send"}.get(action.key, "run"), ""


def simulate(
    workspace_id: uuid.UUID, definition: dict[str, Any], contact_id: uuid.UUID
) -> list[Visit]:
    contact = contacts.serialise_contacts(
        workspace_id, [contacts.get_contact(workspace_id, contact_id)]
    )[0]
    steps = {str(s["id"]): s for s in definition["steps"]}
    current: str | None = str(definition["start"])
    visits: list[Visit] = []
    while current and len(visits) < WALK_LIMIT:
        step = steps[current]
        action = actions.get(str(step["type"]))
        if action is None:
            break
        config = action.config_model.model_validate(step.get("config") or {})
        note, goto = _decide(action, config, contact)
        visits.append(Visit(current, str(step["type"]), note))
        if note == "finish":
            break
        current = goto or step.get("next")
    return visits
