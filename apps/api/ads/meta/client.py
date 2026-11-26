"""Graph / Marketing API client for one Meta ad account.

Authentication is a system-user token; when the connection also stores the app secret every call
carries ``appsecret_proof``. Usage headers feed the shared rate limiter so all workers slow down
together. Meta has no idempotency key, so creates that are retried look for an existing object by
name first (see ``find_by_name``).
"""

import hashlib
import hmac
import json
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import httpx
from django.conf import settings

from ads.meta.credentials import app_credentials
from connections import ratelimit
from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import (
    AuthExpiredError,
    NotFoundError,
    PermanentProviderError,
    PolicyRejectedError,
    RateLimitedError,
    RetryableProviderError,
)

PROVIDER = "meta"
GRAPH_HOST = "https://graph.facebook.com"
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
ACCOUNT_BUCKET = ratelimit.BucketSpec(capacity=30, refill_per_second=5.0)
USAGE_HEADERS = ("x-business-use-case-usage", "x-ad-account-usage", "x-app-usage")
USAGE_PAUSE_AT = 90
USAGE_PAUSE_SECONDS = 60.0
MAX_ERROR_CHARS = 300

AUTH_CODES = {190, 102}
THROTTLE_CODES = {4, 17, 32, 613, 80000, 80003, 80004, 80005, 80006, 80008, 80014}
TRANSIENT_CODES = {1, 2}
POLICY_CODES = {368}

Throttle = Callable[[Connection], None]


def redis_throttle(connection: Connection) -> None:
    decision = ratelimit.acquire(connection.pk, "account", ACCOUNT_BUCKET)
    if not decision.allowed:
        raise RateLimitedError("local rate limit reached", provider=PROVIDER)


def appsecret_proof(token: str, app_secret: str) -> str:
    return hmac.new(app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def peak_usage(raw: str) -> int:
    """Highest percentage in a usage header (any nesting, any metric)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return 0
    peak = 0

    def walk(node: Any) -> None:
        nonlocal peak
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"call_count", "total_cputime", "total_time"} and isinstance(value, int):
                    peak = max(peak, value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return peak


class MetaAdsClient:
    def __init__(
        self,
        connection: Connection,
        *,
        token: str | None = None,
        app_secret: str | None = None,
        version: str | None = None,
        transport: httpx.BaseTransport | None = None,
        throttle: Throttle = redis_throttle,
    ) -> None:
        self._connection = connection
        self._token = token
        self._app_secret = app_secret
        self._secret_loaded = app_secret is not None
        self._throttle = throttle
        self.account_id = connection.external_account_id
        base = f"{GRAPH_HOST}/{version or settings.PROVIDER_API_VERSIONS['meta']}"
        self._http = httpx.Client(base_url=base, timeout=TIMEOUT, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MetaAdsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _credentials(self) -> tuple[str, str | None]:
        if self._token is None:
            self._token = read_secret(self._connection, CredentialKind.ACCESS_TOKEN)
        if not self._secret_loaded:
            try:
                self._app_secret = read_secret(self._connection, CredentialKind.CLIENT_SECRET)
            except NotFoundError:
                # Sign-in connections keep no secret of their own; the workspace's Meta app has it.
                self._app_secret = app_credentials(self._connection.workspace_id).app_secret or None
            self._secret_loaded = True
        return self._token, self._app_secret

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._throttle(self._connection)
        token, secret = self._credentials()
        query = dict(params or {})
        if secret:
            query["appsecret_proof"] = appsecret_proof(token, secret)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            response = self._http.request(
                method, path, params=query, json=json_body, headers=headers
            )
        except httpx.TransportError as exc:
            raise RetryableProviderError(
                f"network error: {type(exc).__name__}", provider=PROVIDER
            ) from exc
        self._note_usage(response)
        return self._parse(response)

    def _note_usage(self, response: httpx.Response) -> None:
        peak = max((peak_usage(response.headers.get(h, "")) for h in USAGE_HEADERS), default=0)
        if peak >= USAGE_PAUSE_AT:
            ratelimit.penalize(self._connection.pk, "account", seconds=USAGE_PAUSE_SECONDS)

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code < HTTPStatus.BAD_REQUEST:
            body = response.json() if response.content else {}
            return body if isinstance(body, dict) else {"data": body}
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        code = error.get("code") if isinstance(error, dict) else None
        message = (
            (error.get("error_user_msg") or error.get("message") or response.text)
            if isinstance(error, dict)
            else response.text
        )
        detail = f"HTTP {response.status_code} code {code}: {str(message)[:MAX_ERROR_CHARS]}"
        if code in AUTH_CODES or response.status_code == HTTPStatus.UNAUTHORIZED:
            raise AuthExpiredError(detail, provider=PROVIDER)
        if code in THROTTLE_CODES or response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            ratelimit.penalize(self._connection.pk, "account", seconds=USAGE_PAUSE_SECONDS)
            raise RateLimitedError(detail, provider=PROVIDER)
        if code in POLICY_CODES:
            raise PolicyRejectedError(detail, provider=PROVIDER)
        if code in TRANSIENT_CODES or response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise RetryableProviderError(detail, provider=PROVIDER)
        raise PermanentProviderError(detail, provider=PROVIDER)

    # --- reads -----------------------------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        fields = "name,currency,account_status,disable_reason,timezone_name,spend_cap,amount_spent"
        return self.request("GET", f"/{self.account_id}", params={"fields": fields})

    def get_object(self, object_id: str, fields: str) -> dict[str, Any]:
        return self.request("GET", f"/{object_id}", params={"fields": fields})

    def list_edge(
        self, parent: str, edge: str, fields: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"fields": fields, "limit": limit}
            if after:
                params["after"] = after
            page = self.request("GET", f"/{parent}/{edge}", params=params)
            rows.extend(page.get("data", []))
            cursors = (page.get("paging") or {}).get("cursors") or {}
            if not (page.get("paging") or {}).get("next") or not cursors.get("after"):
                return rows
            after = cursors["after"]

    def find_by_name(self, edge: str, name: str) -> dict[str, Any] | None:
        """Look for an object created by an earlier attempt of the same action."""
        rows = self.request(
            "GET",
            f"/{self.account_id}/{edge}",
            params={
                "fields": "id,name,status",
                "limit": 5,
                "filtering": json.dumps([{"field": "name", "operator": "EQUAL", "value": name}]),
            },
        ).get("data", [])
        return rows[0] if rows else None

    def insights(
        self, object_id: str, *, level: str, date_from: str, date_to: str, daily: bool = True
    ) -> list[dict[str, Any]]:
        """Delivery rows for the period, following the cursor until the last page."""
        fields = (
            "campaign_id,adset_id,ad_id,spend,impressions,reach,frequency,clicks,ctr,cpc,cpm,"
            "actions,action_values,cost_per_action_type,account_currency"
        )
        params: dict[str, Any] = {
            "level": level,
            "fields": fields,
            "time_range": json.dumps({"since": date_from, "until": date_to}),
            "limit": 500,
        }
        if daily:
            params["time_increment"] = 1
        rows: list[dict[str, Any]] = []
        while True:
            page = self.request("GET", f"/{object_id}/insights", params=params)
            rows.extend(page.get("data", []))
            paging = page.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next") or not after:
                return rows
            params = {**params, "after": after}

    # --- writes ----------------------------------------------------------------------------

    def create(
        self, edge: str, body: dict[str, Any], *, validate_only: bool = False
    ) -> dict[str, Any]:
        payload = dict(body)
        if validate_only:
            payload["execution_options"] = ["validate_only"]
        return self.request("POST", f"/{self.account_id}/{edge}", json_body=payload)

    def update(self, object_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/{object_id}", json_body=body)

    def delete(self, object_id: str) -> dict[str, Any]:
        return self.request("DELETE", f"/{object_id}")

    def upload_image(self, name: str, content_base64: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/{self.account_id}/adimages",
            json_body={"name": name, "bytes": content_base64},
        )
