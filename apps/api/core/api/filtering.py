"""Whitelisted filtering and ordering for list endpoints. Unknown fields are rejected."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from django.db.models import QuerySet

from core.errors import ValidationFailedError

Caster = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class FilterSpec:
    lookup: str
    cast: Caster = str


def apply_filters[Q: QuerySet[Any]](
    queryset: Q, params: Mapping[str, str], allowed: Mapping[str, FilterSpec]
) -> Q:
    """Apply ``params`` using only the lookups declared in ``allowed``."""
    unknown = set(params) - set(allowed)
    if unknown:
        raise ValidationFailedError(f"unknown filters: {', '.join(sorted(unknown))}")
    conditions: dict[str, Any] = {}
    for name, raw in params.items():
        spec = allowed[name]
        try:
            conditions[spec.lookup] = spec.cast(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationFailedError(f"invalid value for {name}") from exc
    return queryset.filter(**conditions)


def apply_ordering[Q: QuerySet[Any]](
    queryset: Q, ordering: str | None, allowed: Mapping[str, str], default: str = "-created_at"
) -> Q:
    """Order by a public name mapped to a model field; a leading ``-`` means descending."""
    requested = ordering or default
    descending = requested.startswith("-")
    name = requested.removeprefix("-")
    if name not in allowed:
        raise ValidationFailedError(f"cannot order by {name}")
    return queryset.order_by(f"{'-' if descending else ''}{allowed[name]}")
