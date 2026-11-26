"""Input cleaning for pipeline, stage and deal fields. Pure functions; each raises on bad input."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from core.errors import ValidationFailedError
from crm.pipelines import rules
from crm.pipelines.models import DealPriority

NAME_LIMIT = 100
DEAL_NAME_LIMIT = 200
SOURCE_LIMIT = 100
TAG_LIMIT = 30
MAX_TAGS = 10
MAX_PROBABILITY = 100
MAX_ROTTING_DAYS = 365
CURRENCY_LENGTH = 3


def name(value: str, limit: int = NAME_LIMIT) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > limit:
        raise ValidationFailedError("invalid_name")
    return cleaned


def deal_name(value: str) -> str:
    return name(value, DEAL_NAME_LIMIT)


def probability(value: int) -> int:
    if not 0 <= value <= MAX_PROBABILITY:
        raise ValidationFailedError("invalid_probability")
    return value


def probability_override(value: int | None) -> int | None:
    return None if value is None else probability(value)


def value_micros(value: int) -> int:
    if value < 0:
        raise ValidationFailedError("invalid_value")
    return value


def currency(value: str) -> str:
    code = value.strip().upper()
    if len(code) != CURRENCY_LENGTH or not code.isalpha():
        raise ValidationFailedError("invalid_currency")
    return code


def source(value: str) -> str:
    return value.strip()[:SOURCE_LIMIT]


def priority(value: str) -> str:
    if value not in DealPriority.values:
        raise ValidationFailedError("invalid_priority")
    return value


def tags(values: Iterable[str]) -> list[str]:
    """Trimmed, de-duplicated (case-insensitive), in the order given."""
    seen: dict[str, str] = {}
    for raw in values:
        tag = " ".join(raw.split())
        if not tag:
            continue
        if len(tag) > TAG_LIMIT:
            raise ValidationFailedError("invalid_tag")
        seen.setdefault(tag.lower(), tag)
    if len(seen) > MAX_TAGS:
        raise ValidationFailedError("too_many_tags")
    return list(seen.values())


def close_date(value: date | None) -> date | None:
    return value


def rotting_days(value: int | None) -> int | None:
    if value is None:
        return None
    if not 1 <= value <= MAX_ROTTING_DAYS:
        raise ValidationFailedError("invalid_rotting_days")
    return value


def required_fields(values: Iterable[str]) -> list[str]:
    keys = list(dict.fromkeys(values))
    if not all(rules.is_known(key) for key in keys):
        raise ValidationFailedError("invalid_requirement")
    return keys
