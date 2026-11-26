"""One place that builds WordPress clients."""

from connections.models import Connection
from content.wordpress.client import WordPressClient


def for_connection(connection: Connection) -> WordPressClient:
    return WordPressClient.for_connection(connection)


def build(site_url: str, *, username: str, password: str) -> WordPressClient:
    return WordPressClient(site_url, username=username, password=password)
