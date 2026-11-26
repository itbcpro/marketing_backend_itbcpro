"""Outbound fetch for site work. Tests replace ``transport`` and ``resolver`` to stay offline."""

from typing import Any

import httpx

from core.security import Resolver, safe_fetch
from core.security import _default_resolver as default_resolver

transport: httpx.BaseTransport | None = None
resolver: Resolver | None = None


def fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    max_bytes: int = 4_000_000,
    method: str = "GET",
    json_body: Any = None,
    content: bytes | None = None,
) -> httpx.Response:
    chosen = resolver if resolver is not None else default_resolver
    return safe_fetch(
        url,
        resolver=chosen,
        headers=headers,
        timeout=timeout,
        max_bytes=max_bytes,
        transport=transport,
        method=method,
        json_body=json_body,
        content=content,
    )
