"""Stage entry rules: what a deal must have before it may enter a stage.

A requirement is a named predicate. Add one by adding an entry to ``REQUIREMENTS``; stages store the
names they demand, so nothing else changes.
"""

from __future__ import annotations

from collections.abc import Callable

from crm.pipelines.models import Opportunity, Stage

Requirement = Callable[[Opportunity], bool]

REQUIREMENTS: dict[str, Requirement] = {
    "value": lambda deal: deal.value_micros > 0,
    "expected_close_date": lambda deal: deal.expected_close_date is not None,
    "owner": lambda deal: deal.assigned_to_id is not None,
}


def is_known(key: str) -> bool:
    return key in REQUIREMENTS


def available() -> list[str]:
    return list(REQUIREMENTS)


def missing(deal: Opportunity, stage: Stage) -> list[str]:
    """Requirements of ``stage`` that ``deal`` does not meet yet."""
    return [
        key for key in stage.required_fields if key in REQUIREMENTS and not REQUIREMENTS[key](deal)
    ]
