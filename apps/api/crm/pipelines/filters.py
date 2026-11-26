"""Deal filters and sort orders.

Both are registries: a new filter or sort is a new entry, and nothing else changes. Filters are
applied from the keys a caller gives; an unknown key is refused rather than ignored.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from django.db.models import Q, QuerySet

from core.errors import ValidationFailedError
from crm.pipelines.models import Opportunity

Rows = QuerySet[Opportunity]
Apply = Callable[[Rows, Any], Rows]


def _equals(field: str) -> Apply:
    return lambda rows, value: rows.filter(**{field: value})


def _compare(field: str, lookup: str) -> Apply:
    return lambda rows, value: rows.filter(**{f"{field}__{lookup}": value})


def _search(rows: Rows, text: str) -> Rows:
    term = text.strip()
    if not term:
        return rows
    return rows.filter(
        Q(name__icontains=term)
        | Q(contact__first_name__icontains=term)
        | Q(contact__last_name__icontains=term)
        | Q(contact__email__icontains=term)
    )


FILTERS: dict[str, Apply] = {
    "pipeline_id": _equals("pipeline_id"),
    "stage_id": _equals("stage_id"),
    "status": _equals("status"),
    "contact_id": _equals("contact_id"),
    "assigned_to": _equals("assigned_to_id"),
    "priority": _equals("priority"),
    "source": _compare("source", "iexact"),
    "tag": lambda rows, tag: rows.filter(tags__contains=[tag]),
    "unassigned": lambda rows, on: rows.filter(assigned_to__isnull=True) if on else rows,
    "min_value_micros": _compare("value_micros", "gte"),
    "max_value_micros": _compare("value_micros", "lte"),
    "created_after": _compare("created_at", "gte"),
    "created_before": _compare("created_at", "lt"),
    "close_after": _compare("expected_close_date", "gte"),
    "close_before": _compare("expected_close_date", "lte"),
    "q": _search,
}

# Sort key -> ordering. A leading "-" from the caller flips it.
SORTS: dict[str, tuple[str, ...]] = {
    "updated": ("updated_at",),
    "created": ("created_at",),
    "moved": ("last_stage_change_at",),
    "value": ("value_micros",),
    "name": ("name",),
    "close_date": ("expected_close_date",),
}
DEFAULT_SORT = "-updated"


def apply_filters(rows: Rows, criteria: Mapping[str, Any]) -> Rows:
    """Apply every given criterion; ``None`` means "not given"."""
    for key, value in criteria.items():
        if key not in FILTERS:
            raise ValidationFailedError("unknown_filter", errors={key: "unknown_filter"})
        if value is not None:
            rows = FILTERS[key](rows, value)
    return rows


def apply_sort(rows: Rows, sort: str | None) -> Rows:
    key = sort or DEFAULT_SORT
    descending = key.startswith("-")
    fields = SORTS.get(key.lstrip("-"))
    if fields is None:
        raise ValidationFailedError("unknown_sort", errors={"sort": "unknown_sort"})
    order = [f"-{f}" if descending else f for f in fields]
    # Deals without a date go last either way; the id keeps the order stable between pages.
    return rows.order_by(*order, "-id" if descending else "id")
