"""REST client for the Google Ads API (one customer account per instance).

Calls carry the OAuth access token, the developer token and, for accounts reached through a
manager, ``login-customer-id``. An expired access token is refreshed once and the call repeated.
Failures are mapped to domain errors so the action layer can decide between retry and give-up.
"""

import re
import uuid
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import httpx
from django.conf import settings

from ads.google.credentials import app_credentials
from connections import ratelimit
from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    PolicyRejectedError,
    RateLimitedError,
    RetryableProviderError,
)

PROVIDER = "google_ads"
HOST = "https://googleads.googleapis.com"
TIMEOUT = httpx.Timeout(60.0, connect=5.0)
ACCOUNT_BUCKET = ratelimit.BucketSpec(capacity=30, refill_per_second=5.0)
MAX_ERROR_CHARS = 400
DIGITS = re.compile(r"\D")
TRANSIENT_STATUSES = {"ABORTED", "UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"}
POLICY_KEYS = {"policyViolationError", "policyFindingError", "adError"}

Throttle = Callable[[Connection], None]
Refresher = Callable[[Connection], bool]


def customer_id(value: str) -> str:
    """Account ids appear as 123-456-7890 in the UI; the API wants digits only."""
    return DIGITS.sub("", value)


def api_version() -> str:
    return str(settings.PROVIDER_API_VERSIONS.get("google_ads", "v25"))


def redis_throttle(connection: Connection) -> None:
    decision = ratelimit.acquire(connection.pk, "account", ACCOUNT_BUCKET)
    if not decision.allowed:
        raise RateLimitedError("local rate limit reached", provider=PROVIDER)


def _refresh(connection: Connection) -> bool:
    from connections import lifecycle

    return lifecycle.refresh_connection(connection)


def _failure_text(payload: dict[str, Any]) -> str:
    """One line from a Google error body: message plus the failing field path when present."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    message = str(error.get("message") or error.get("status") or "")
    for detail in error.get("details") or []:
        for item in (detail or {}).get("errors") or []:
            path = ".".join(
                str(p.get("fieldName", ""))
                for p in (item.get("location") or {}).get("fieldPathElements") or []
            )
            text = str(item.get("message") or "")
            if text:
                message = f"{text} ({path})" if path else text
                break
    return message[:MAX_ERROR_CHARS]


def _error_keys(payload: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    error = payload.get("error") if isinstance(payload, dict) else None
    for detail in (error or {}).get("details") or []:
        for item in (detail or {}).get("errors") or []:
            keys.update((item.get("errorCode") or {}).keys())
    return keys


CONCURRENT_EDIT = "attempting to modify the same resource at once"


def raise_for_response(response: httpx.Response) -> None:
    if response.status_code < HTTPStatus.BAD_REQUEST:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    status_name = (
        str((payload.get("error") or {}).get("status", "")) if isinstance(payload, dict) else ""
    )
    keys = _error_keys(payload if isinstance(payload, dict) else {})
    text = _failure_text(payload) or response.text[:MAX_ERROR_CHARS]
    detail = f"HTTP {response.status_code} {status_name}: {text}"
    if response.status_code == HTTPStatus.UNAUTHORIZED or status_name == "UNAUTHENTICATED":
        raise AuthExpiredError(detail, provider=PROVIDER)
    if (
        response.status_code == HTTPStatus.TOO_MANY_REQUESTS
        or "quotaError" in keys
        or status_name == "RESOURCE_EXHAUSTED"
    ):
        raise RateLimitedError(detail, provider=PROVIDER)
    if keys & POLICY_KEYS and "policy" in detail.lower():
        raise PolicyRejectedError(detail, provider=PROVIDER)
    if (
        response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
        or status_name in TRANSIENT_STATUSES
        or "internalError" in keys
        or CONCURRENT_EDIT in detail  # parallel edits of one account: the retry succeeds
    ):
        raise RetryableProviderError(detail, provider=PROVIDER)
    raise PermanentProviderError(detail, provider=PROVIDER)


class GoogleAdsClient:
    def __init__(
        self,
        connection: Connection,
        *,
        access_token: str | None = None,
        developer_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        throttle: Throttle = redis_throttle,
        refresher: Refresher = _refresh,
    ) -> None:
        self._connection = connection
        self._token = access_token
        self._developer_token = developer_token
        self._throttle = throttle
        self._refresher = refresher
        self.customer = customer_id(connection.external_account_id)
        self.login_customer = customer_id(str(connection.config.get("login_customer_id", "")))
        self._http = httpx.Client(
            base_url=f"{HOST}/{api_version()}", timeout=TIMEOUT, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GoogleAdsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            self._token = read_secret(self._connection, CredentialKind.ACCESS_TOKEN)
        headers = {"Authorization": f"Bearer {self._token}"}
        developer = (
            self._developer_token or app_credentials(self._connection.workspace_id).developer_token
        )
        # Google no longer requires a developer token; older projects may still send one.
        if developer:
            headers["developer-token"] = developer
        if self.login_customer:
            headers["login-customer-id"] = self.login_customer
        return headers

    def request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._throttle(self._connection)
        for attempt in range(2):
            try:
                response = self._http.request(method, path, json=json_body, headers=self._headers())
            except httpx.TransportError as exc:
                raise RetryableProviderError(
                    f"network error: {type(exc).__name__}", provider=PROVIDER
                ) from exc
            if (
                response.status_code == HTTPStatus.UNAUTHORIZED
                and attempt == 0
                and self._refresher(self._connection)
            ):
                self._token = None  # re-read the refreshed token
                continue
            raise_for_response(response)
            body = response.json() if response.content else {}
            return body if isinstance(body, dict) else {"results": body}
        raise AuthExpiredError("the access token could not be refreshed", provider=PROVIDER)

    # --- reads -----------------------------------------------------------------------------

    def list_accessible(self) -> list[str]:
        body = self.request("GET", "/customers:listAccessibleCustomers")
        return [customer_id(name) for name in body.get("resourceNames", [])]

    def search(self, query: str, *, cid: str | None = None) -> list[dict[str, Any]]:
        """All rows of a GAQL query, following page tokens."""
        rows: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            payload: dict[str, Any] = {"query": query}  # recent API versions fix the page size
            if token:
                payload["pageToken"] = token
            page = self.request(
                "POST", f"/customers/{cid or self.customer}/googleAds:search", json_body=payload
            )
            rows.extend(page.get("results", []))
            token = page.get("nextPageToken")
            if not token:
                return rows

    # --- writes ----------------------------------------------------------------------------

    def mutate(
        self, service: str, operations: list[dict[str, Any]], *, validate_only: bool = False
    ) -> dict[str, Any]:
        """``service`` is the REST collection, for example ``campaigns`` or ``adGroupAds``."""
        body: dict[str, Any] = {"operations": operations}
        if validate_only:
            body["validateOnly"] = True
        return self.request("POST", f"/customers/{self.customer}/{service}:mutate", json_body=body)

    def mutate_all(
        self, operations: list[dict[str, Any]], *, validate_only: bool = False
    ) -> dict[str, Any]:
        """Several resource types in one all-or-nothing request (temporary negative ids allowed)."""
        body: dict[str, Any] = {"mutateOperations": operations}
        if validate_only:
            body["validateOnly"] = True
        return self.request("POST", f"/customers/{self.customer}/googleAds:mutate", json_body=body)

    def resource(self, collection: str, resource_id: str | int) -> str:
        return f"customers/{self.customer}/{collection}/{resource_id}"


def list_accessible_customers(
    access_token: str,
    *,
    workspace_id: uuid.UUID | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """Accounts the signed-in user can reach; used right after OAuth, before a connection."""
    developer = app_credentials(workspace_id).developer_token
    headers = {"Authorization": f"Bearer {access_token}"}
    if developer:
        headers["developer-token"] = developer
    with httpx.Client(
        base_url=f"{HOST}/{api_version()}", timeout=TIMEOUT, transport=transport
    ) as http:
        response = http.get("/customers:listAccessibleCustomers", headers=headers)
    raise_for_response(response)
    return [customer_id(name) for name in response.json().get("resourceNames", [])]
