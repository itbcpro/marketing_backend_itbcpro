"""One place that builds Google clients, so tests can swap in a fake transport."""

from collections.abc import Callable

from ads.google.client import GoogleAdsClient
from connections.models import Connection

ClientFactory = Callable[[Connection], GoogleAdsClient]


def _default(connection: Connection) -> GoogleAdsClient:
    return GoogleAdsClient(connection)


client_factory: ClientFactory = _default
"""Replaced in tests; production always builds a real client."""


def open_client(connection: Connection) -> GoogleAdsClient:
    return client_factory(connection)
