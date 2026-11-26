"""HTTP client for the OpenAI Advertiser API (https://developers.openai.com/ads).

One instance talks to one ad account: the API key is scoped to it. Every call passes the shared
per-connection rate limiter; throttling and auth failures are mapped to domain errors so the
action layer can decide between retry and give-up.
"""

import json
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import httpx

from connections import ratelimit
from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)

PROVIDER = "openai_ads"
BASE_URL = "https://api.ads.openai.com/v1"
TIMEOUT = httpx.Timeout(20.0, connect=5.0)
# Documented limits: 600 requests/min per endpoint and 1,200/min per account. The account bucket is
# the tighter shared budget, so one bucket per connection is enough.
ACCOUNT_BUCKET = ratelimit.BucketSpec(capacity=60, refill_per_second=20.0)
DEFAULT_RETRY_AFTER = 5.0
MAX_ERROR_CHARS = 300

RETRYABLE_STATUSES = {
    HTTPStatus.REQUEST_TIMEOUT,
    HTTPStatus.CONFLICT,
    HTTPStatus.TOO_EARLY,
}

Throttle = Callable[[Connection], None]


def redis_throttle(connection: Connection) -> None:
    decision = ratelimit.acquire(connection.pk, "account", ACCOUNT_BUCKET)
    if not decision.allowed:
        raise RateLimitedError("local rate limit reached", provider=PROVIDER)


class OpenAiAdsClient:
    def __init__(
        self,
        connection: Connection,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        throttle: Throttle = redis_throttle,
    ) -> None:
        self._connection = connection
        self._api_key = api_key
        self._throttle = throttle
        self._http = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OpenAiAdsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _key(self) -> str:
        if self._api_key is None:
            self._api_key = read_secret(self._connection, CredentialKind.API_KEY)
        return self._api_key

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._throttle(self._connection)
        headers = {"Authorization": f"Bearer {self._key()}", "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._http.request(method, path, json=json, params=params, headers=headers)
        except httpx.TransportError as exc:
            raise RetryableProviderError(
                f"network error: {type(exc).__name__}", provider=PROVIDER
            ) from exc
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if status < HTTPStatus.BAD_REQUEST:
            body = response.json() if response.content else {}
            return body if isinstance(body, dict) else {"data": body}
        detail = f"HTTP {status}: {response.text[:MAX_ERROR_CHARS]}"
        if status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            raise AuthExpiredError(detail, provider=PROVIDER)
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            wait = _retry_after(response)
            ratelimit.penalize(self._connection.pk, "account", seconds=wait)
            raise RateLimitedError(detail, provider=PROVIDER)
        if status in RETRYABLE_STATUSES or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise RetryableProviderError(detail, provider=PROVIDER)
        raise PermanentProviderError(detail, provider=PROVIDER)

    # --- reads -----------------------------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        return self.request("GET", "/ad_account")

    def list_resource(
        self,
        resource: str,
        *,
        parent: tuple[str, str] | None = None,
        limit: int = 50,
        include: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """All rows of ``campaigns``, ``ad_groups`` or ``ads`` (cursor-paginated).

        ``include`` asks the platform for extra diagnostics such as ``serving_issues`` and
        ``bid_too_low``.
        """
        rows: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": limit, "order": "asc"}
            if include:
                params["include[]"] = include
            if parent:
                params[parent[0]] = parent[1]
            if after:
                params["after"] = after
            page = self.request("GET", f"/{resource}", params=params)
            rows.extend(page.get("data", []))
            if not page.get("has_more") or not page.get("last_id"):
                return rows
            after = page["last_id"]

    def geo_lookup(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Targetable places matching a search text: countries, regions and postal areas."""
        page = self.request("GET", "/geo_lookup/search", params={"q": query, "limit": limit})
        rows = page.get("results", page.get("data", []))
        return [row for row in rows if isinstance(row, dict) and row.get("id")]

    def get_resource(self, resource: str, resource_id: str) -> dict[str, Any]:
        return self.request("GET", f"/{resource}/{resource_id}")

    def insights(
        self,
        level: str,
        entity_id: str | None,
        *,
        date_from: str,
        date_to: str,
        granularity: str = "daily",
        segment: str | None = None,
        aggregation_level: str | None = None,
    ) -> list[dict[str, Any]]:
        """Delivery metrics for a window. ``level`` is account, campaign, ad_group or ad."""
        path = {
            "account": "/ad_account/insights",
            "campaign": f"/campaigns/{entity_id}/insights",
            "ad_group": f"/ad_groups/{entity_id}/insights",
            "ad": f"/ads/{entity_id}/insights",
        }[level]
        window = json.dumps({"type": "date_range", "since": date_from, "until": date_to})
        params = [("time_granularity", granularity), ("time_ranges[]", window), ("limit", 2000)]
        if segment:
            params.append(("segments[]", segment))
        if aggregation_level:
            params.append(("aggregation_level", aggregation_level))
        return list(self.request("GET", path, params=params).get("data", []))

    # --- writes ----------------------------------------------------------------------------

    def create(
        self, resource: str, body: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return self.request("POST", f"/{resource}", json=body, idempotency_key=idempotency_key)

    def update(
        self, resource: str, resource_id: str, body: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return self.request(
            "POST", f"/{resource}/{resource_id}", json=body, idempotency_key=idempotency_key
        )

    def set_state(self, resource: str, resource_id: str, state: str) -> dict[str, Any]:
        """``state`` is one of activate, pause or archive. Repeating a transition is harmless."""
        return self.request("POST", f"/{resource}/{resource_id}/{state}")

    def ad_daily_metrics(self, since: str, until: str) -> list[dict[str, Any]]:
        """One row per ad per day for the whole account (finest level; coarser ones are sums)."""
        window = json.dumps({"type": "date_range", "since": since, "until": until})
        rows: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: list[tuple[str, Any]] = [
                ("time_granularity", "daily"),
                ("aggregation_level", "ad"),
                ("fields[]", "metadata.readable_time"),
                ("fields[]", "ad.id"),
                ("fields[]", "ad.impressions"),
                ("fields[]", "ad.clicks"),
                ("fields[]", "ad.spend"),
                ("time_ranges[]", window),
                ("limit", 2000),
            ]
            if after:
                params.append(("after", after))
            page = self.request("GET", "/ad_account/insights", params=params)
            rows.extend(page.get("data", []))
            if not page.get("has_more") or not page.get("last_id"):
                return rows
            after = page["last_id"]

    def spend_limits(self) -> dict[str, Any]:
        """Account spending limits. Needs billing permission on the key, so callers handle 403."""
        return self.request("GET", "/ad_account/spend_limit_windows")

    def preview_ad(self, ad_id: str) -> dict[str, Any]:
        return self.request("POST", f"/ads/{ad_id}/preview")

    def upload_image(self, image_url: str, *, idempotency_key: str) -> dict[str, Any]:
        return self.request(
            "POST", "/upload", json={"image_url": image_url}, idempotency_key=idempotency_key
        )


def identify_account(api_key: str) -> tuple[str, str]:
    """Return ``(ad account id, display name)`` for a key, or raise when the key is rejected."""
    response = httpx.get(
        f"{BASE_URL}/ad_account",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
        raise AuthExpiredError("the API key was rejected", provider=PROVIDER)
    if response.status_code != HTTPStatus.OK:
        raise PermanentProviderError(f"HTTP {response.status_code}", provider=PROVIDER)
    account = response.json()
    name = account.get("account_name") or account.get("name") or account["id"]
    return str(account["id"]), str(name)


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(1.0, float(response.headers.get("Retry-After", DEFAULT_RETRY_AFTER)))
    except ValueError:
        return DEFAULT_RETRY_AFTER
