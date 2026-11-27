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


_account_factories: dict[str, Callable[[object], ChannelAdapter]] = {}


def register_account_adapter(provider: str, factory: Callable[[object], ChannelAdapter]) -> None:
    """Adapters that depend on which connected account sends (a person's own mailbox)."""
    _account_factories[provider] = factory


def adapter_for_account(provider: str, account: object) -> ChannelAdapter | None:
    factory = _account_factories.get(provider)
    return factory(account) if factory else None


def channels() -> list[str]:
    return sorted(_factories)
