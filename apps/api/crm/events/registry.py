"""Registry of event types: each type and version has exactly one pydantic data model."""

from collections.abc import Callable

from pydantic import BaseModel

_registry: dict[tuple[str, int], type[BaseModel]] = {}


def event(event_type: str, version: int = 1) -> Callable[[type[BaseModel]], type[BaseModel]]:
    """Class decorator that registers the data model of an event type."""

    def register(model: type[BaseModel]) -> type[BaseModel]:
        key = (event_type, version)
        if key in _registry:
            raise ValueError(f"event {event_type} v{version} is already registered")
        _registry[key] = model
        return model

    return register


def data_model(event_type: str, version: int) -> type[BaseModel]:
    try:
        return _registry[(event_type, version)]
    except KeyError as exc:
        raise KeyError(f"event {event_type} v{version} is not registered") from exc


def all_events() -> dict[tuple[str, int], type[BaseModel]]:
    return dict(sorted(_registry.items()))
