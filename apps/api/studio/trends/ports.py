"""Port and registry for trend sources.

A source kind turns validated parameters into a list of raw signals. The collector knows nothing
about feeds, search engines or discussion sites; adding a source is registering one class.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ValidationError

from core.errors import ValidationFailedError


@dataclass(frozen=True, slots=True)
class RawTrend:
    title: str
    summary: str = ""
    url: str = ""
    published_at: datetime | None = None
    score: float = 0.0


class TrendSourceKind(Protocol):
    kind: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]
    needs_network: ClassVar[bool]

    def default_name(self, params: BaseModel) -> str: ...

    def fetch(self, workspace_id: uuid.UUID, params: Any) -> list[RawTrend]: ...


_KINDS: dict[str, TrendSourceKind] = {}


def register_trend_source(source: TrendSourceKind) -> None:
    _KINDS[source.kind] = source


def get_trend_source(kind: str) -> TrendSourceKind:
    found = _KINDS.get(kind)
    if found is None:
        raise ValidationFailedError(f"unknown trend source: {kind}")
    return found


def all_trend_sources() -> list[TrendSourceKind]:
    return list(_KINDS.values())


def validate_params(kind: str, params: dict[str, Any]) -> BaseModel:
    model = get_trend_source(kind).params_model
    try:
        return model.model_validate(params)
    except ValidationError as exc:
        fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
        raise ValidationFailedError(f"check these source settings: {fields}") from exc


def rank_score(index: int, total: int) -> float:
    """Feeds list the most important first, so position is the only signal many of them give."""
    return round(1.0 - index / max(total, 1), 3)
