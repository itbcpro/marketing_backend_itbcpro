"""Which adapter serves which channel. Adapters register themselves at startup."""

from __future__ import annotations

from collections.abc import Callable

from crm.sending.ports import ChannelAdapter

AdapterFactory = Callable[[], ChannelAdapter]

_factories: dict[str, AdapterFactory] = {}


def register(channel: str, factory: AdapterFactory) -> None:
    """Register (or replace) the adapter of a channel. Replacing is how tests inject fakes."""
    _factories[channel] = factory


def unregister(channel: str) -> None:
    _factories.pop(channel, None)


def adapter_for(channel: str) -> ChannelAdapter | None:
    factory = _factories.get(channel)
    return factory() if factory else None


def channels() -> list[str]:
    return sorted(_factories)
