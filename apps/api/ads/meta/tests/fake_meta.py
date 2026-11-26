"""A small in-memory Meta ad account for tests: one campaign, one ad set, one ad."""

import json
from datetime import timedelta
from typing import Any

import httpx
from django.utils import timezone

ACCOUNT = "act_555"


class FakeMeta:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []
        self.fail: dict[str, tuple[int, dict[str, Any]]] = {}
        self.created_campaigns: list[dict[str, Any]] = []
        self.next_id = 900
        self.account_status = 1
        self.learning = "LEARNING"
        self.pixels: list[dict[str, Any]] = []

    def _days(self, count: int = 3) -> list[str]:
        today = timezone.now().date()
        return [(today - timedelta(days=i)).isoformat() for i in range(count)]

    def _new_id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def campaigns(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "10",
                "name": "Spring offer",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
                "objective": "OUTCOME_LEADS",
                "daily_budget": "2000",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "special_ad_categories": [],
            },
            *self.created_campaigns,
        ]

    def read(self, path: str, params: dict[str, str]) -> dict[str, Any]:  # noqa: PLR0911
        if path == f"/{ACCOUNT}":
            return {
                "id": ACCOUNT,
                "name": "Acme ads",
                "currency": "USD",
                "account_status": self.account_status,
                "timezone_name": "America/Vancouver",
                "spend_cap": "100000",
                "amount_spent": "95000",
            }
        if path == f"/{ACCOUNT}/campaigns":
            wanted = params.get("filtering")
            rows = self.campaigns()
            if wanted:
                name = json.loads(wanted)[0]["value"]
                rows = [r for r in rows if r["name"] == name]
            return {"data": rows}
        if path == f"/{ACCOUNT}/adsets":
            return {
                "data": [
                    {
                        "id": "20",
                        "campaign_id": "10",
                        "name": "Vancouver audience",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                        "optimization_goal": "OFFSITE_CONVERSIONS",
                        "billing_event": "IMPRESSIONS",
                        "targeting": {"geo_locations": {"countries": ["CA"]}},
                        "learning_stage_info": {"status": self.learning},
                    }
                ]
            }
        if path == f"/{ACCOUNT}/ads":
            return {
                "data": [
                    {
                        "id": "30",
                        "adset_id": "20",
                        "name": "Backups made easy",
                        "status": "ACTIVE",
                        "effective_status": "DISAPPROVED",
                        "ad_review_feedback": {"global": {"Personal attributes": "Do not imply"}},
                        "creative": {
                            "id": "40",
                            "title": "Stop worrying about backups",
                            "body": "We test your backups every week.",
                            "object_type": "SHARE",
                            "image_url": "https://cdn.example.com/a.jpg",
                            "call_to_action_type": "LEARN_MORE",
                            "object_story_spec": {
                                "page_id": "77",
                                "link_data": {"link": "https://acme.example/backups"},
                            },
                        },
                    }
                ]
            }
        if path == f"/{ACCOUNT}/adspixels":
            return {"data": self.pixels}
        if path == f"/{ACCOUNT}/promote_pages":
            return {"data": [{"id": "77", "name": "Acme"}]}
        if path == f"/{ACCOUNT}/instagram_accounts":
            return {"data": [{"id": "88", "username": "acme"}]}
        if path == f"/{ACCOUNT}/insights":
            return {
                "data": [
                    {
                        "ad_id": "30",
                        "date_start": day,
                        "spend": "2.50",
                        "impressions": "100",
                        "clicks": "5",
                        "actions": [
                            {"action_type": "lead", "value": "2"},
                            {"action_type": "offsite_conversion.fb_pixel_lead", "value": "2"},
                            {"action_type": "link_click", "value": "5"},
                        ],
                        "action_values": [],
                    }
                    for day in self._days()
                ]
            }
        return {}

    def write(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        edge = path.rsplit("/", 1)[-1]
        if method == "DELETE":
            self.deleted.append(path.lstrip("/"))
            return {"success": True}
        if path == f"/{ACCOUNT}/adimages":
            return {"images": {body["name"]: {"hash": "hash123"}}}
        if path.startswith(f"/{ACCOUNT}/") and edge in {
            "campaigns",
            "adsets",
            "adcreatives",
            "ads",
        }:
            if "execution_options" in body:
                return {"success": True}
            new_id = self._new_id()
            if edge == "campaigns":
                self.created_campaigns.append(
                    {
                        "id": new_id,
                        "name": body["name"],
                        "status": "PAUSED",
                        "effective_status": "PAUSED",
                        "objective": body["objective"],
                        "daily_budget": body["daily_budget"],
                    }
                )
            return {"id": new_id}
        return {"success": True}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = "/" + request.url.path.split("/", 2)[2]  # drop the version segment
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, params))
        for key, (status, payload) in self.fail.items():
            if path.endswith(key) and request.method != "GET":
                return httpx.Response(status, json=payload)
        if request.method == "GET":
            return httpx.Response(200, json=self.read(path, params))
        self.posts.append((path, body))
        return httpx.Response(200, json=self.write(request.method, path, body))
