"""Client for the Instagram API with Instagram Login (graph.instagram.com), one account each.

Reads and writes use the long-lived user token as a bearer header. Every call first takes a token
from the shared per-connection bucket; usage headers and throttling errors slow every worker down.
Platform failures become domain errors so the action layer can choose between retry and give up.
"""

import json
from collections.abc import Callable, Iterator
from http import HTTPStatus
from typing import Any

import httpx
from django.conf import settings

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

PROVIDER = "instagram"
HOST = "https://graph.instagram.com"
TIMEOUT = httpx.Timeout(45.0, connect=5.0)
ACCOUNT_BUCKET = ratelimit.BucketSpec(capacity=40, refill_per_second=5.0)
USAGE_HEADERS = ("x-business-use-case-usage", "x-app-usage")
USAGE_PAUSE_AT = 90
USAGE_PAUSE_SECONDS = 60.0
MAX_ERROR_CHARS = 300
PAGE_SIZE = 50

AUTH_CODES = {190, 102, 459, 463, 467}
THROTTLE_CODES = {4, 17, 32, 613, 80002, 80006, 80007}
TRANSIENT_CODES = {1, 2}
POLICY_CODES = {368}
TRANSIENT_SUBCODES = {2207027, 2207053}  # media is not ready yet

MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,"
    "like_count,comments_count,is_comment_enabled,alt_text,"
    "children{id,media_type,media_url,thumbnail_url}"
)
ACCOUNT_FIELDS = (
    "user_id,username,name,account_type,profile_picture_url,followers_count,follows_count,"
    "media_count,biography,website"
)
COMMENT_FIELDS = (
    "id,text,username,timestamp,like_count,hidden,parent_id,from{id,username},"
    "replies{id,text,username,timestamp,like_count,hidden,from{id,username}}"
)
MESSAGE_FIELDS = (
    "id,created_time,from,to,message,attachments{id,mime_type,name,image_data,video_data}"
)

Throttle = Callable[[Connection], None]


def api_version() -> str:
    return str(settings.PROVIDER_API_VERSIONS.get("instagram", "v26.0"))


def redis_throttle(connection: Connection) -> None:
    decision = ratelimit.acquire(connection.pk, "account", ACCOUNT_BUCKET)
    if not decision.allowed:
        raise RateLimitedError("local rate limit reached", provider=PROVIDER)


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


def raise_for_response(response: httpx.Response) -> None:
    if response.status_code < HTTPStatus.BAD_REQUEST:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    sub = error.get("error_subcode")
    message = str(error.get("error_user_msg") or error.get("message") or response.text)
    detail = f"HTTP {response.status_code} code {code}/{sub}: {message[:MAX_ERROR_CHARS]}"
    if code in AUTH_CODES or response.status_code == HTTPStatus.UNAUTHORIZED:
        raise AuthExpiredError(detail, provider=PROVIDER)
    if code in THROTTLE_CODES or response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        raise RateLimitedError(detail, provider=PROVIDER)
    if code in POLICY_CODES:
        raise PolicyRejectedError(detail, provider=PROVIDER)
    if (
        code in TRANSIENT_CODES
        or sub in TRANSIENT_SUBCODES
        or error.get("is_transient")
        or response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
    ):
        raise RetryableProviderError(detail, provider=PROVIDER)
    raise PermanentProviderError(detail, provider=PROVIDER)


def _metric_values(page: dict[str, Any]) -> dict[str, float]:
    """``{metric: number}`` from an insights response (total_value or first value)."""
    found: dict[str, float] = {}
    for item in page.get("data", []):
        name = item.get("name")
        if not name:
            continue
        total = item.get("total_value")
        if isinstance(total, dict) and "value" in total:
            found[name] = float(total["value"] or 0)
            continue
        values = item.get("values") or []
        if values and isinstance(values[0].get("value"), int | float):
            found[name] = float(values[0]["value"])
    return found


class InstagramClient:
    def __init__(
        self,
        connection: Connection,
        *,
        access_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        throttle: Throttle = redis_throttle,
    ) -> None:
        self._connection = connection
        self._token = access_token
        self._throttle = throttle
        self.account_id = connection.external_account_id
        self._http = httpx.Client(
            base_url=f"{HOST}/{api_version()}", timeout=TIMEOUT, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> InstagramClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._throttle(self._connection)
        if self._token is None:
            self._token = read_secret(self._connection, CredentialKind.ACCESS_TOKEN)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            response = self._http.request(method, path, params=params, json=body, headers=headers)
        except httpx.TransportError as exc:
            raise RetryableProviderError(
                f"network error: {type(exc).__name__}", provider=PROVIDER
            ) from exc
        peak = max((peak_usage(response.headers.get(h, "")) for h in USAGE_HEADERS), default=0)
        if peak >= USAGE_PAUSE_AT:
            ratelimit.penalize(self._connection.pk, "account", seconds=USAGE_PAUSE_SECONDS)
        try:
            raise_for_response(response)
        except RateLimitedError:
            ratelimit.penalize(self._connection.pk, "account", seconds=USAGE_PAUSE_SECONDS)
            raise
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {"data": payload}

    def pages(
        self, path: str, params: dict[str, Any], *, max_items: int
    ) -> Iterator[dict[str, Any]]:
        """Items of a paged edge, following cursors until ``max_items`` were yielded."""
        seen = 0
        query = {**params, "limit": min(PAGE_SIZE, max_items)}
        while True:
            page = self.request("GET", path, params=query)
            for item in page.get("data", []):
                yield item
                seen += 1
                if seen >= max_items:
                    return
            paging = page.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next") or not after:
                return
            query = {**query, "after": after}

    # --- account and media reads -----------------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/me", params={"fields": ACCOUNT_FIELDS})

    def list_media(self, *, max_items: int = 100) -> list[dict[str, Any]]:
        return list(
            self.pages(f"/{self.account_id}/media", {"fields": MEDIA_FIELDS}, max_items=max_items)
        )

    def list_stories(self) -> list[dict[str, Any]]:
        fields = "id,media_type,media_url,thumbnail_url,permalink,timestamp,caption"
        return list(self.pages(f"/{self.account_id}/stories", {"fields": fields}, max_items=50))

    def list_tags(self, *, max_items: int = 30) -> list[dict[str, Any]]:
        fields = "id,caption,media_type,permalink,timestamp,username,media_url,thumbnail_url"
        return list(self.pages(f"/{self.account_id}/tags", {"fields": fields}, max_items=max_items))

    def media_insights(self, media_id: str, metrics: list[str]) -> dict[str, float]:
        page = self.request("GET", f"/{media_id}/insights", params={"metric": ",".join(metrics)})
        return _metric_values(page)

    def account_totals(self, metrics: list[str], *, since: int, until: int) -> dict[str, float]:
        page = self.request(
            "GET",
            f"/{self.account_id}/insights",
            params={
                "metric": ",".join(metrics),
                "period": "day",
                "metric_type": "total_value",
                "since": since,
                "until": until,
            },
        )
        return _metric_values(page)

    def account_series(self, metric: str, *, since: int, until: int) -> list[dict[str, Any]]:
        page = self.request(
            "GET",
            f"/{self.account_id}/insights",
            params={
                "metric": metric,
                "period": "day",
                "metric_type": "time_series",
                "since": since,
                "until": until,
            },
        )
        for item in page.get("data", []):
            if item.get("name") == metric:
                return [v for v in item.get("values", []) if isinstance(v, dict)]
        return []

    def demographics(self, metric: str, breakdown: str) -> list[dict[str, Any]]:
        page = self.request(
            "GET",
            f"/{self.account_id}/insights",
            params={
                "metric": metric,
                "period": "lifetime",
                "timeframe": "last_30_days",
                "metric_type": "total_value",
                "breakdown": breakdown,
            },
        )
        rows: list[dict[str, Any]] = []
        for item in page.get("data", []):
            breakdowns = (item.get("total_value") or {}).get("breakdowns") or [{}]
            for result in breakdowns[0].get("results", []):
                key = "|".join(result.get("dimension_values", []))
                rows.append({"key": key, "value": result.get("value", 0)})
        return rows

    def online_followers(self) -> dict[str, Any]:
        page = self.request(
            "GET",
            f"/{self.account_id}/insights",
            params={"metric": "online_followers", "period": "lifetime"},
        )
        for item in page.get("data", []):
            values = item.get("values") or []
            if values:
                return dict(values[-1].get("value") or {})
        return {}

    # --- comments and messages -------------------------------------------------------------

    def list_comments(self, media_id: str, *, max_items: int = 100) -> list[dict[str, Any]]:
        return list(
            self.pages(f"/{media_id}/comments", {"fields": COMMENT_FIELDS}, max_items=max_items)
        )

    def list_conversations(self, *, max_items: int = 50) -> list[dict[str, Any]]:
        params = {
            "platform": "instagram",
            "fields": f"id,updated_time,participants,messages.limit(20){{{MESSAGE_FIELDS}}}",
        }
        return list(self.pages(f"/{self.account_id}/conversations", params, max_items=max_items))

    def reply_to_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        return self.request("POST", f"/{comment_id}/replies", params={"message": text})

    def comment_replies(self, comment_id: str) -> list[dict[str, Any]]:
        return list(
            self.pages(
                f"/{comment_id}/replies", {"fields": "id,text,username,timestamp"}, max_items=50
            )
        )

    def hide_comment(self, comment_id: str, *, hidden: bool) -> dict[str, Any]:
        return self.request("POST", f"/{comment_id}", params={"hide": str(hidden).lower()})

    def delete_comment(self, comment_id: str) -> dict[str, Any]:
        return self.request("DELETE", f"/{comment_id}")

    def post_comment(self, media_id: str, text: str) -> dict[str, Any]:
        return self.request("POST", f"/{media_id}/comments", params={"message": text})

    def set_comments_enabled(self, media_id: str, *, enabled: bool) -> dict[str, Any]:
        return self.request(
            "POST", f"/{media_id}", params={"comment_enabled": str(enabled).lower()}
        )

    def send_message(self, recipient_id: str, text: str) -> dict[str, Any]:
        body = {"recipient": {"id": recipient_id}, "message": {"text": text}}
        return self.request("POST", f"/{self.account_id}/messages", body=body)

    def send_private_reply(self, comment_id: str, text: str) -> dict[str, Any]:
        body = {"recipient": {"comment_id": comment_id}, "message": {"text": text}}
        return self.request("POST", f"/{self.account_id}/messages", body=body)

    def subscribe_webhooks(self, fields: list[str]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/{self.account_id}/subscribed_apps",
            params={"subscribed_fields": ",".join(fields)},
        )

    # --- publishing ------------------------------------------------------------------------

    def create_container(self, fields: dict[str, Any]) -> str:
        page = self.request("POST", f"/{self.account_id}/media", body=fields)
        return str(page["id"])

    def container_status(self, container_id: str) -> dict[str, Any]:
        return self.request("GET", f"/{container_id}", params={"fields": "status_code,status"})

    def publish_container(self, container_id: str) -> str:
        page = self.request(
            "POST", f"/{self.account_id}/media_publish", body={"creation_id": container_id}
        )
        return str(page["id"])

    def publishing_limit(self) -> dict[str, Any]:
        page = self.request(
            "GET",
            f"/{self.account_id}/content_publishing_limit",
            params={"fields": "quota_usage,config"},
        )
        rows = page.get("data") or [{}]
        return dict(rows[0]) if rows else {}

    def media_detail(self, media_id: str) -> dict[str, Any]:
        return self.request("GET", f"/{media_id}", params={"fields": MEDIA_FIELDS})
