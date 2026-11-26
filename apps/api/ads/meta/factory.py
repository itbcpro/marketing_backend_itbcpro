"""One place that builds Meta clients, so tests can swap in a fake transport."""

from collections.abc import Callable

from ads.meta.client import MetaAdsClient
from connections.models import Connection

ClientFactory = Callable[[Connection], MetaAdsClient]


def _default(connection: Connection) -> MetaAdsClient:
    return MetaAdsClient(connection)


client_factory: ClientFactory = _default
"""Replaced in tests; production always builds a real client."""


def open_client(connection: Connection) -> MetaAdsClient:
    return client_factory(connection)
