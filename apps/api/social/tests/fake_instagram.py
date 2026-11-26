"""A small stateful stand-in for graph.instagram.com, enough to drive sync, replies and publishing."""

import json
import re
from typing import Any

import httpx

ACCOUNT_ID = "17841400000000000"


def _error(
    code: int, message: str, status: int = 400, subcode: int | None = None
) -> httpx.Response:
    body = {"error": {"message": message, "type": "OAuthException", "code": code}}
    if subcode:
        body["error"]["error_subcode"] = subcode
    return httpx.Response(status, json=body)


class FakeInstagram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.account: dict[str, Any] = {
            "user_id": ACCOUNT_ID,
            "id": "9999",
            "username": "acme_it",
            "name": "Acme IT",
            "account_type": "BUSINESS",
            "profile_picture_url": "https://cdn.example/p.jpg",
            "followers_count": 1200,
            "follows_count": 90,
            "media_count": 4,
            "biography": "Managed IT",
            "website": "https://acme.example",
        }
        self.media: list[dict[str, Any]] = []
        self.stories: list[dict[str, Any]] = []
        self.insights: dict[str, dict[str, float]] = {}
        self.unsupported_metrics: set[str] = set()
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.conversations: list[dict[str, Any]] = []
        self.account_totals: dict[str, float] = {
            "reach": 5000,
            "views": 9000,
            "accounts_engaged": 300,
        }
        self.follower_series: list[int] = [1100, 1150, 1200]
        self.quota: dict[str, Any] = {"quota_usage": 3, "config": {"quota_total": 50}}
        self.container_states: dict[str, list[str]] = {}
        self.default_container_states = ["IN_PROGRESS", "FINISHED"]
        self.containers: dict[str, dict[str, Any]] = {}
        self.published: list[str] = []
        self.fail_next: list[httpx.Response] = []
        self.next_id = 1000
        self.message_counter = 0

    # --- helpers ---------------------------------------------------------------------------

    def new_id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def calls_to(self, method: str, pattern: str) -> list[dict[str, Any]]:
        return [d for m, p, d in self.calls if m == method and re.search(pattern, p)]

    def add_media(self, media_id: str, **fields: Any) -> dict[str, Any]:
        item = {
            "id": media_id,
            "media_type": "IMAGE",
            "media_product_type": "FEED",
            "caption": "A post",
            "permalink": f"https://instagram.com/p/{media_id}",
            "timestamp": "2026-09-20T10:00:00+0000",
            "like_count": 10,
            "comments_count": 0,
            "is_comment_enabled": True,
            **fields,
        }
        self.media.append(item)
        self.insights.setdefault(
            media_id,
            {
                "reach": 400,
                "likes": 10,
                "comments": 1,
                "saved": 5,
                "shares": 2,
                "total_interactions": 20,
                "views": 600,
            },
        )
        return item

    # --- transport -------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = re.sub(r"^/v[\d.]+", "", request.url.path)
        params = dict(request.url.params)
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, {**params, **body}))
        if self.fail_next:
            return self.fail_next.pop(0)
        return self.route(request.method, path, params, body)

    def route(  # noqa: C901, PLR0911 - a router is a list of cases
        self, method: str, path: str, params: dict[str, str], body: dict[str, Any]
    ) -> httpx.Response:
        if method == "GET" and path == "/me":
            return httpx.Response(200, json=self.account)
        if path == f"/{ACCOUNT_ID}/media" and method == "GET":
            return httpx.Response(200, json={"data": self.media})
        if path == f"/{ACCOUNT_ID}/media" and method == "POST":
            return self.create_container(body)
        if path == f"/{ACCOUNT_ID}/media_publish":
            return self.publish(body)
        if path == f"/{ACCOUNT_ID}/stories":
            return httpx.Response(200, json={"data": self.stories})
        if path == f"/{ACCOUNT_ID}/insights":
            return self.account_insights(params)
        if path == f"/{ACCOUNT_ID}/content_publishing_limit":
            return httpx.Response(200, json={"data": [self.quota]})
        if path == f"/{ACCOUNT_ID}/conversations":
            return httpx.Response(200, json={"data": self.conversations})
        if path == f"/{ACCOUNT_ID}/messages":
            return self.send_message(body)
        if path == f"/{ACCOUNT_ID}/subscribed_apps":
            return httpx.Response(200, json={"success": True})
        return self.route_objects(method, path, params)

    def route_objects(  # noqa: PLR0911 - a router is a list of cases
        self, method: str, path: str, params: dict[str, str]
    ) -> httpx.Response:
        parts = path.strip("/").split("/")
        object_id = parts[0]
        edge = parts[1] if len(parts) > 1 else ""
        if edge == "insights":
            return self.media_insights(object_id, params)
        if edge == "comments" and method == "GET":
            return httpx.Response(200, json={"data": self.comments.get(object_id, [])})
        if edge == "comments":
            return httpx.Response(200, json={"id": self.new_id()})
        if edge == "replies" and method == "GET":
            return httpx.Response(200, json={"data": self.replies_of(object_id)})
        if edge == "replies":
            return self.add_reply(object_id, params)
        if method == "DELETE":
            return httpx.Response(200, json={"success": True})
        if method == "POST":
            return httpx.Response(200, json={"success": True})
        if object_id in self.containers:
            return self.container_status(object_id)
        found = next((m for m in self.media if m["id"] == object_id), None)
        if found is not None:
            return httpx.Response(200, json=found)
        return _error(100, "Unsupported get request", 400)

    def replies_of(self, comment_id: str) -> list[dict[str, Any]]:
        for items in self.comments.values():
            for item in items:
                if item["id"] == comment_id:
                    return list((item.get("replies") or {}).get("data", []))
        return []

    def add_reply(self, comment_id: str, params: dict[str, str]) -> httpx.Response:
        reply = {
            "id": self.new_id(),
            "text": params.get("message", ""),
            "username": "acme_it",
            "timestamp": "2026-09-21T10:00:00+0000",
        }
        for items in self.comments.values():
            for item in items:
                if item["id"] == comment_id:
                    item.setdefault("replies", {"data": []})["data"].append(reply)
        return httpx.Response(200, json={"id": reply["id"]})

    def media_insights(self, media_id: str, params: dict[str, str]) -> httpx.Response:
        wanted = params.get("metric", "").split(",")
        if media_id == ACCOUNT_ID:
            return self.account_insights(params)
        if self.unsupported_metrics & set(wanted):
            return _error(100, "metric not supported for this media")
        values = self.insights.get(media_id, {})
        data = [{"name": m, "values": [{"value": values[m]}]} for m in wanted if m in values]
        return httpx.Response(200, json={"data": data})

    def account_insights(self, params: dict[str, str]) -> httpx.Response:
        wanted = params.get("metric", "").split(",")
        if params.get("metric_type") == "time_series":
            values = [
                {"value": v, "end_time": f"2026-09-{20 + i}T07:00:00+0000"}
                for i, v in enumerate(self.follower_series)
            ]
            return httpx.Response(200, json={"data": [{"name": wanted[0], "values": values}]})
        if "demographics" in params.get("metric", ""):
            return _error(100, "not enough followers")
        data = [
            {"name": m, "total_value": {"value": self.account_totals[m]}}
            for m in wanted
            if m in self.account_totals
        ]
        return httpx.Response(200, json={"data": data})

    def create_container(self, body: dict[str, Any]) -> httpx.Response:
        container_id = self.new_id()
        self.containers[container_id] = body
        self.container_states[container_id] = list(self.default_container_states)
        return httpx.Response(200, json={"id": container_id})

    def container_status(self, container_id: str) -> httpx.Response:
        states = self.container_states[container_id]
        state = states.pop(0) if len(states) > 1 else states[0]
        return httpx.Response(200, json={"id": container_id, "status_code": state, "status": state})

    def publish(self, body: dict[str, Any]) -> httpx.Response:
        container = self.containers.get(str(body.get("creation_id")))
        if container is None:
            return _error(100, "unknown container")
        media_id = self.new_id()
        self.published.append(media_id)
        self.container_states[str(body["creation_id"])] = ["PUBLISHED"]
        self.add_media(
            media_id,
            caption=container.get("caption", ""),
            media_type="VIDEO" if container.get("media_type") == "REELS" else "IMAGE",
            media_product_type="REELS" if container.get("media_type") == "REELS" else "FEED",
        )
        return httpx.Response(200, json={"id": media_id})

    def send_message(self, body: dict[str, Any]) -> httpx.Response:
        self.message_counter += 1
        return httpx.Response(
            200,
            json={
                "recipient_id": body["recipient"].get("id", ""),
                "message_id": f"m-out-{self.message_counter}",
            },
        )
