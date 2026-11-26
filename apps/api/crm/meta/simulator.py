"""A stand-in for the parts of Meta's Graph API the CRM uses. It never leaves the process."""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx


class SimulatedGraph:
    """Accepts messages like the real API and can be made to fail with a Graph error code."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.failures: list[tuple[int, int]] = []  # (http status, graph error code)
        self.phone_info: dict[str, Any] = {
            "display_phone_number": "+1 416-555-0100",
            "quality_rating": "GREEN",
            "messaging_limit_tier": "TIER_1K",
        }
        self.exchanged = "long-lived-value"
        # Lead ad submissions by id, as Meta would return them.
        self.leads: dict[str, dict[str, Any]] = {}
        # What Meta lists for the account; tests put approved or rejected templates here.
        self.templates: list[dict[str, Any]] = []
        self._counter = itertools.count(1)

    def fail_next(self, http_status: int, code: int) -> None:
        self.failures.append((http_status, code))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body or dict(request.url.params)))
        if self.failures:
            status, code = self.failures.pop(0)
            return httpx.Response(status, json={"error": {"code": code, "message": "simulated"}})
        return httpx.Response(200, json=self._answer(request, body))

    def _answer(self, request: httpx.Request, body: dict[str, Any]) -> dict[str, Any]:
        path = request.url.path
        n = next(self._counter)
        answers: list[tuple[bool, dict[str, Any]]] = [
            (path.endswith("/oauth/access_token"), {"access_token": self.exchanged}),
            (path.endswith("/subscribed_apps"), {"success": True}),
            (
                path.endswith("/message_templates") and request.method == "GET",
                {"data": self.templates},
            ),
            (
                path.endswith("/message_templates") and request.method == "POST",
                {"id": f"TPL{n:06d}", "status": "PENDING", "category": body.get("category", "")},
            ),
            (path.endswith("/message_templates"), {"success": True}),
            (path.rsplit("/", 1)[-1] in self.leads, self.leads.get(path.rsplit("/", 1)[-1], {})),
            (request.method == "GET", self.phone_info),
            (
                path.endswith("/messages") and body.get("messaging_product") == "whatsapp",
                {"messages": [{"id": f"wamid.SIM{n:08d}"}]},
            ),
            (path.endswith("/messages"), {"message_id": f"m_SIM{n:08d}"}),
        ]
        return next((answer for matches, answer in answers if matches), {"id": f"SIM{n:08d}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)
