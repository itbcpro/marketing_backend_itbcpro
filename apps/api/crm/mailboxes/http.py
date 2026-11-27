"""The one place HTTP clients for mail providers are made (tests replace :func:`client`)."""

from __future__ import annotations

import httpx

TIMEOUT_SECONDS = 20


def client() -> httpx.Client:
    """A client that never follows redirects, so a token or mail never goes to a surprise host."""
    return httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)
