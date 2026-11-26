"""A small filter language for contacts, compiled to ORM queries through a whitelist.

A filter is a group ``{"op": "and"|"or", "conditions": [...]}`` whose items are conditions
``{"field": ..., "operator": ..., "value": ...}`` or nested groups. Nothing is interpolated into
SQL: every field and operator comes from the tables below, unknown ones are rejected.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Lower

from core.errors import ValidationFailedError
from crm.contacts.models import ContactTag, ContactType
from crm.fields import services as fields

TEXT_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "address",
    "city",
    "state",
    "postal_code",
    "country",
    "source",
)
TEXT_OPERATORS = {"eq", "neq", "contains", "starts_with", "empty", "not_empty"}
TIME_FIELDS = ("created_at", "updated_at", "last_activity_at")
TIME_OPERATORS = {"before", "after", "empty", "not_empty"}
ID_FIELDS = {"assigned_to": "assigned_to_id", "company_id": "company_id"}
ID_OPERATORS = {"eq", "neq", "empty", "not_empty"}
TAG_OPERATORS = {"has", "not_has", "has_any"}
MAX_DEPTH = 5
CUSTOM_PREFIX = "custom."


class Compiler:
    """Turns one filter into a ``Q`` for a given workspace, counting conditions as it goes."""

    def __init__(self, workspace_id: uuid.UUID) -> None:
        self.workspace_id = workspace_id
        self.count = 0

    def compile(self, node: dict[str, Any], depth: int = 0) -> Q:
        if depth > MAX_DEPTH:
            raise _bad("filter", "too_deep")
        if "conditions" in node:
            return self._group(node, depth)
        self.count += 1
        if self.count > settings.CRM_FILTER_MAX_CONDITIONS:
            raise _bad("filter", "too_many_conditions")
        return self._condition(node)

    def _group(self, node: dict[str, Any], depth: int) -> Q:
        op, items = node.get("op", "and"), node.get("conditions")
        if op not in {"and", "or"} or not isinstance(items, list):
            raise _bad("filter", "invalid_group")
        parts = [self.compile(item, depth + 1) for item in items if isinstance(item, dict)]
        if len(parts) != len(items):
            raise _bad("filter", "invalid_group")
        combined = Q()
        for part in parts:
            combined = combined & part if op == "and" else combined | part
        return combined

    def _condition(self, node: dict[str, Any]) -> Q:
        field, operator, value = node.get("field"), node.get("operator"), node.get("value")
        if not isinstance(field, str) or not isinstance(operator, str):
            raise _bad("filter", "invalid_condition")
        try:
            return self._build(field, operator, value)
        except (ValueError, TypeError) as exc:
            raise _bad(field, "invalid_value") from exc

    def _build(self, field: str, operator: str, value: Any) -> Q:
        if field in TEXT_FIELDS:
            return _text(field, _allowed(field, operator, TEXT_OPERATORS), value)
        if field == "contact_type":
            return _enum(operator, value)
        if field in ID_FIELDS:
            return _ids(ID_FIELDS[field], _allowed(field, operator, ID_OPERATORS), value)
        if field in TIME_FIELDS:
            return _time(field, _allowed(field, operator, TIME_OPERATORS), value)
        if field == "tags":
            return self._tags(_allowed(field, operator, TAG_OPERATORS), value)
        if field.startswith(CUSTOM_PREFIX):
            ids, negate = fields.matching_record_ids(
                self.workspace_id, "contact", field.removeprefix(CUSTOM_PREFIX), operator, value
            )
            return ~Q(pk__in=ids) if negate else Q(pk__in=ids)
        raise _bad(field, "unknown_field")

    def _tags(self, operator: str, value: Any) -> Q:
        names = [str(v).lower() for v in (value if isinstance(value, list) else [value])]
        if operator == "has" and len(names) != 1:
            raise _bad("tags", "invalid_value")
        links = ContactTag.objects.filter(workspace_id=self.workspace_id)
        ids = links.annotate(low=Lower("tag__name")).filter(low__in=names).values("contact_id")
        return ~Q(pk__in=ids) if operator == "not_has" else Q(pk__in=ids)


def _bad(field: str, reason: str) -> ValidationFailedError:
    return ValidationFailedError("invalid filter", errors={field: reason})


def _allowed(field: str, operator: str, allowed: set[str]) -> str:
    if operator not in allowed:
        raise _bad(field, "invalid_operator")
    return operator


def _text(field: str, operator: str, value: Any) -> Q:
    lookups = {
        "eq": Q(**{f"{field}__iexact": value}),
        "neq": ~Q(**{f"{field}__iexact": value}),
        "contains": Q(**{f"{field}__icontains": value}),
        "starts_with": Q(**{f"{field}__istartswith": value}),
        "empty": Q(**{field: ""}),
        "not_empty": ~Q(**{field: ""}),
    }
    return lookups[operator]


def _enum(operator: str, value: Any) -> Q:
    values = value if isinstance(value, list) else [value]
    if operator not in {"eq", "neq", "in"} or not set(values) <= set(ContactType.values):
        raise _bad("contact_type", "invalid_value")
    match = Q(contact_type__in=values)
    return ~match if operator == "neq" else match


def _ids(column: str, operator: str, value: Any) -> Q:
    if operator in {"empty", "not_empty"}:
        return Q(**{f"{column}__isnull": operator == "empty"})
    match = Q(**{column: uuid.UUID(str(value))})
    return ~match if operator == "neq" else match


def _time(field: str, operator: str, value: Any) -> Q:
    if operator in {"empty", "not_empty"}:
        return Q(**{f"{field}__isnull": operator == "empty"})
    moment = datetime.fromisoformat(str(value))
    return Q(**{f"{field}__{'lt' if operator == 'before' else 'gt'}": moment})


def compile_filter(workspace_id: uuid.UUID, spec: dict[str, Any] | None) -> Q:
    """Compile a stored or submitted filter. An empty filter matches everything."""
    if not spec:
        return Q()
    return Compiler(workspace_id).compile(spec)
