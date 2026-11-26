"""A stand-in for Twilio message API for local development and CI; it never leaves the process."""

from __future__ import annotations

import itertools
from urllib.parse import parse_qs

import httpx

from crm.sms.twilio_client import IDEMPOTENCY_HEADER


class SimulatedTwilio:
    """Accepts messages like the real API, honours idempotency tokens, and can be made to fail."""

    def __init__(self) -> None:
        self.messages: list[dict[str, list[str]]] = []
        self.failures: list[tuple[int, int | None]] = []  # (http status, twilio error code)
        self._by_token: dict[str, str] = {}
        self._counter = itertools.count(1)

    def fail_next(self, http_status: int, code: int | None = None) -> None:
        self.failures.append((http_status, code))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.failures:
            status, code = self.failures.pop(0)
            return httpx.Response(status, json={"code": code, "message": "simulated failure"})
        token = request.headers.get(IDEMPOTENCY_HEADER, "")
        if token and token in self._by_token:
            return httpx.Response(201, json={"sid": self._by_token[token], "status": "queued"})
        form = parse_qs(request.content.decode())
        sid = f"SM{next(self._counter):032d}"
        self.messages.append(form)
        if token:
            self._by_token[token] = sid
        return httpx.Response(201, json={"sid": sid, "status": "queued"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)
