"""One place that builds Instagram clients, so tests can swap in a fake transport."""

from collections.abc import Callable

from connections.models import Connection
from social.instagram.client import InstagramClient

ClientFactory = Callable[[Connection], InstagramClient]


def _default(connection: Connection) -> InstagramClient:
    return InstagramClient(connection)


client_factory: ClientFactory = _default
"""Replaced in tests; production always builds a real client."""


def open_client(connection: Connection) -> InstagramClient:
    return client_factory(connection)
