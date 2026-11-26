"""Conditions of branch steps: a few comparisons on a contact, combined with all or any."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

TAG_OPERATORS = frozenset({"has_tag", "not_has_tag"})
CUSTOM_PREFIX = "custom."


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = ""
    op: Literal[
        "equals", "not_equals", "contains", "has_tag", "not_has_tag", "is_set", "is_not_set"
    ]
    value: str = ""


def _value_of(contact: dict[str, Any], field: str) -> Any:
    if field.startswith(CUSTOM_PREFIX):
        return contact.get("custom", {}).get(field[len(CUSTOM_PREFIX) :])
    return contact.get(field)


def _has_tag(condition: Condition, tags: list[Any], present: bool) -> bool:
    has = condition.value.lower() in {str(t).lower() for t in tags}
    return has if present else not has


def check(condition: Condition, contact: dict[str, Any]) -> bool:
    """Evaluate one condition against a serialised contact."""
    op = condition.op
    if op in TAG_OPERATORS:
        return _has_tag(condition, contact.get("tags", []), op == "has_tag")
    actual = _value_of(contact, condition.field)
    text = "" if actual is None else str(actual).lower()
    wanted = condition.value.lower()
    tests = {
        "is_set": text != "",
        "is_not_set": text == "",
        "equals": text == wanted,
        "not_equals": text != wanted,
        "contains": wanted in text,
    }
    return tests.get(op, False)


def evaluate(conditions: list[Condition], match: str, contact: dict[str, Any]) -> bool:
    results = [check(c, contact) for c in conditions]
    return any(results) if match == "any" else all(results)
