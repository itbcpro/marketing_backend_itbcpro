"""Client for the Search Console API (read only): sites, sitemaps and search analytics.

Requests go to Google's fixed host with the connection's access token, refreshed first when it is
about to expire. Failures become domain errors so a sync can retry or give up.
"""

from contextlib import suppress
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from django.utils import timezone

from connections import lifecycle
from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)

PROVIDER = "google_search"
HOST = "https://www.googleapis.com"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
REFRESH_MARGIN = timedelta(minutes=2)
ROW_LIMIT = 25_000
HTTP_ERROR = 400
HTTP_UNAUTHORISED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY = 429
HTTP_SERVER_ERROR = 500

transport: httpx.BaseTransport | None = None
"""Replaced in tests with an ``httpx.MockTransport``; production leaves it unset."""


def http_client() -> httpx.Client:
    return httpx.Client(base_url=HOST, timeout=TIMEOUT, transport=transport)


def fresh_access_token(connection: Connection) -> str:
    """The access token, refreshed through the connection lifecycle when it is about to expire."""
    expires = connection.expires_at
    if expires is not None and expires <= timezone.now() + REFRESH_MARGIN:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            lifecycle.refresh_connection(connection, client=client)
        connection.refresh_from_db()
    return read_secret(connection, CredentialKind.ACCESS_TOKEN)


def raise_for(response: httpx.Response) -> None:
    status = response.status_code
    if status < HTTP_ERROR:
        return
    reason = ""
    with suppress(ValueError, AttributeError):
        reason = str(response.json().get("error", {}).get("message", ""))[:200]
    if status == HTTP_UNAUTHORISED:
        raise AuthExpiredError("google_unauthorised", provider=PROVIDER)
    if status == HTTP_FORBIDDEN:
        raise PermanentProviderError(reason or "google_forbidden", provider=PROVIDER)
    if status == HTTP_NOT_FOUND:
        raise PermanentProviderError("google_not_found", provider=PROVIDER)
    if status == HTTP_TOO_MANY:
        raise RateLimitedError("google_rate_limited", provider=PROVIDER)
    if status >= HTTP_SERVER_ERROR:
        raise RetryableProviderError("google_unavailable", provider=PROVIDER)
    raise PermanentProviderError(reason or f"google_rejected_{status}", provider=PROVIDER)


class SearchConsoleClient:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {fresh_access_token(self.connection)}"}
        try:
            with http_client() as client:
                response = client.request(method, path, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise RetryableProviderError("google_timeout", provider=PROVIDER) from exc
        except httpx.TransportError as exc:
            raise RetryableProviderError("google_unreachable", provider=PROVIDER) from exc
        raise_for(response)
        return response.json() if response.content else {}

    def list_sites(self) -> list[dict[str, str]]:
        data = self._request("GET", "/webmasters/v3/sites")
        return [
            {"site_url": s["siteUrl"], "permission": s.get("permissionLevel", "")}
            for s in data.get("siteEntry", [])
        ]

    def list_sitemaps(self, site_url: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/webmasters/v3/sites/{quote(site_url, safe='')}/sitemaps")
        return list(data.get("sitemap", []))

    def query(
        self,
        site_url: str,
        *,
        start: date,
        end: date,
        dimensions: list[str],
        start_row: int = 0,
        data_state: str = "final",
    ) -> list[dict[str, Any]]:
        """One page of search analytics rows (up to 25,000)."""
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dimensions,
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
            "type": "web",
            "dataState": data_state,
        }
        path = f"/webmasters/v3/sites/{quote(site_url, safe='')}/searchAnalytics/query"
        return list(self._request("POST", path, body).get("rows", []))

    def all_rows(
        self, site_url: str, *, start: date, end: date, dimensions: list[str]
    ) -> list[dict[str, Any]]:
        """Every row of a query, following pages until one comes back short."""
        rows: list[dict[str, Any]] = []
        while True:
            page = self.query(
                site_url, start=start, end=end, dimensions=dimensions, start_row=len(rows)
            )
            rows.extend(page)
            if len(page) < ROW_LIMIT:
                return rows
