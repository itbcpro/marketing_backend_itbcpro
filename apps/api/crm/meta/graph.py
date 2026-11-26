"""A small Graph API client for the CRM's Meta channels. Tests inject an httpx transport."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx
from django.conf import settings

from crm.meta import errors


class GraphError(Exception):
    def __init__(self, mapped: errors.Mapped) -> None:
        super().__init__(mapped.code)
        self.code = mapped.code
        self.retryable = mapped.retryable


class GraphClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        version = settings.PROVIDER_API_VERSIONS["meta"]
        self._client = httpx.Client(
            base_url=f"{settings.CRM_META_GRAPH_BASE}/{version}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=settings.CRM_META_TIMEOUT_SECONDS,
            transport=transport,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", path, json=body)

    def delete(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._call("DELETE", path, params=params)

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise GraphError(errors.Mapped(errors.UNAVAILABLE, True)) from exc  # never sent
        except httpx.TransportError as exc:
            # The request may have reached Meta. The Cloud API has no idempotency key, so sending
            # again could message the customer twice: report an unknown outcome instead.
            raise GraphError(errors.Mapped(errors.UNKNOWN_OUTCOME, method != "POST")) from exc
        payload = _json(response)
        if response.is_success:
            return payload
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise GraphError(
            errors.from_graph(code if isinstance(code, int) else None, response.status_code)
        )


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def signature_of(app_secret: str, body: bytes) -> str:
    """The ``X-Hub-Signature-256`` value Meta sends for a webhook body."""
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
