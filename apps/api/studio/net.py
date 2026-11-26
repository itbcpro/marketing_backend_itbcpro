"""Outbound fetch for trend sources. Tests replace ``transport`` and ``resolver``."""

import httpx

from core.security import Resolver, safe_fetch
from core.security import _default_resolver as default_resolver

transport: httpx.BaseTransport | None = None
resolver: Resolver | None = None

FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "application/json;q=0.8, */*;q=0.5"
)


def fetch(url: str, *, timeout: float = 15.0, max_bytes: int = 3_000_000) -> httpx.Response:
    """GET ``url`` through the SSRF guard (private addresses and odd ports are refused)."""
    return safe_fetch(
        url,
        resolver=resolver if resolver is not None else default_resolver,
        headers={"Accept": FEED_ACCEPT},
        timeout=timeout,
        max_bytes=max_bytes,
        transport=transport,
    )
