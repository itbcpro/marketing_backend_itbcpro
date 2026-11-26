"""Google PageSpeed Insights (Lighthouse plus real-user Chrome data) for one address.

The API needs no sign-in. An API key (a workspace connection or ``APP_GOOGLE_PAGESPEED_API_KEY``)
only raises the daily quota, which is small without one.
"""

import uuid
from contextlib import suppress
from typing import Any

import httpx
from django.conf import settings

from connections.models import Connection, ConnectionStatus, CredentialKind
from connections.providers import (
    AuthType,
    FormField,
    HealthResult,
    ProviderDescriptor,
    register_provider,
)
from connections.services import read_secret
from core.errors import (
    DomainError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)

PROVIDER = "google_pagespeed"
ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
TIMEOUT = httpx.Timeout(120.0, connect=10.0)
STRATEGIES = ("mobile", "desktop")
CATEGORIES = ("PERFORMANCE", "SEO", "ACCESSIBILITY", "BEST_PRACTICES")
GOOD_AUDIT = 0.9
MAX_OPPORTUNITIES = 8
SCORE_SCALE = 100
CLS_SCALE = 100
HTTP_TOO_MANY = 429
HTTP_SERVER_ERROR = 500

transport: httpx.BaseTransport | None = None
"""Replaced in tests with an ``httpx.MockTransport``."""


def api_key(workspace_id: uuid.UUID | None) -> str:
    connection = (
        Connection.objects.filter(workspace_id=workspace_id, provider=PROVIDER)
        .exclude(status="revoked")
        .first()
        if workspace_id
        else None
    )
    if connection is not None:
        try:
            return read_secret(connection, CredentialKind.API_KEY)
        except DomainError:
            pass
    return settings.GOOGLE_PAGESPEED_API_KEY


def _score(category: dict[str, Any] | None) -> int | None:
    value = (category or {}).get("score")
    return None if value is None else round(float(value) * SCORE_SCALE)


def _numeric(audits: dict[str, Any], name: str) -> float | None:
    value = (audits.get(name) or {}).get("numericValue")
    return None if value is None else round(float(value), 3)


def _lab(audits: dict[str, Any]) -> dict[str, float | None]:
    return {
        "lcp_ms": _numeric(audits, "largest-contentful-paint"),
        "fcp_ms": _numeric(audits, "first-contentful-paint"),
        "cls": _numeric(audits, "cumulative-layout-shift"),
        "tbt_ms": _numeric(audits, "total-blocking-time"),
        "speed_index_ms": _numeric(audits, "speed-index"),
    }


def _field(experience: dict[str, Any]) -> dict[str, Any]:
    metrics = experience.get("metrics") or {}
    if not metrics:
        return {}

    def pick(name: str) -> tuple[Any, str]:
        item = metrics.get(name) or {}
        return item.get("percentile"), str(item.get("category", ""))

    lcp, lcp_cat = pick("LARGEST_CONTENTFUL_PAINT_MS")
    inp, inp_cat = pick("INTERACTION_TO_NEXT_PAINT")
    cls, cls_cat = pick("CUMULATIVE_LAYOUT_SHIFT_SCORE")
    return {
        "category": str(experience.get("overall_category", "")),
        "lcp_ms": lcp,
        "lcp_category": lcp_cat,
        "inp_ms": inp,
        "inp_category": inp_cat,
        "cls": None if cls is None else cls / CLS_SCALE,
        "cls_category": cls_cat,
    }


def _opportunities(audits: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for audit_id, audit in audits.items():
        score = audit.get("score")
        details = audit.get("details") or {}
        if score is None or score >= GOOD_AUDIT:
            continue
        if audit.get("scoreDisplayMode") not in {"numeric", "binary", "metricSavings"}:
            continue
        savings = details.get("overallSavingsMs") or (audit.get("metricSavings") or {}).get("LCP")
        found.append(
            {
                "id": audit_id,
                "title": audit.get("title", audit_id),
                "display": audit.get("displayValue", ""),
                "savings_ms": round(float(savings)) if savings else 0,
                "score": score,
            }
        )
    found.sort(key=lambda a: (-a["savings_ms"], a["score"]))
    return found[:MAX_OPPORTUNITIES]


def parse(payload: dict[str, Any]) -> dict[str, Any]:
    """The numbers worth keeping from one PageSpeed Insights response."""
    lighthouse = payload.get("lighthouseResult") or {}
    categories = lighthouse.get("categories") or {}
    audits = lighthouse.get("audits") or {}
    experience = payload.get("loadingExperience") or {}
    return {
        "performance": _score(categories.get("performance")),
        "seo": _score(categories.get("seo")),
        "accessibility": _score(categories.get("accessibility")),
        "best_practices": _score(categories.get("best-practices")),
        "lab": _lab(audits),
        "field": _field(experience),
        "opportunities": _opportunities(audits),
    }


def run(url: str, strategy: str, *, workspace_id: uuid.UUID | None = None) -> dict[str, Any]:
    if strategy not in STRATEGIES:
        raise PermanentProviderError("unknown device type", provider=PROVIDER)
    params: list[tuple[str, str | int | float | bool | None]] = [
        ("url", url),
        ("strategy", strategy),
    ]
    params += [("category", c) for c in CATEGORIES]
    key = api_key(workspace_id)
    if key:
        params.append(("key", key))
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            response = client.get(ENDPOINT, params=params)
    except httpx.TimeoutException as exc:
        raise RetryableProviderError("PageSpeed timed out", provider=PROVIDER) from exc
    except httpx.HTTPError as exc:
        raise RetryableProviderError("PageSpeed is unreachable", provider=PROVIDER) from exc
    if response.status_code == HTTP_TOO_MANY:
        raise RateLimitedError("PageSpeed quota reached; add an API key", provider=PROVIDER)
    if response.status_code >= HTTP_SERVER_ERROR:
        raise RetryableProviderError(f"HTTP {response.status_code}", provider=PROVIDER)
    if response.status_code != httpx.codes.OK:
        message = ""
        with suppress(ValueError):
            message = str(response.json().get("error", {}).get("message", ""))
        raise PermanentProviderError(
            f"HTTP {response.status_code}: {message[:200]}", provider=PROVIDER
        )
    return parse(response.json())


def identify(_api_key: str) -> tuple[str, str]:
    return "google-pagespeed", "Google PageSpeed API key"


def health_check(connection: Connection) -> HealthResult:
    try:
        read_secret(connection, CredentialKind.API_KEY)
    except DomainError:
        return HealthResult(ConnectionStatus.DEGRADED, "the API key is missing")
    return HealthResult(ConnectionStatus.ACTIVE)


def register() -> None:
    register_provider(
        ProviderDescriptor(
            name=PROVIDER,
            display_name="Google PageSpeed API key",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField(
                    name="api_key",
                    label="PageSpeed Insights API key",
                    secret=True,
                    help_text=(
                        "Optional. Google Cloud console: enable the PageSpeed Insights API and "
                        "create an API key. Without one the free daily quota is very small."
                    ),
                ),
            ),
            identify=identify,
            health_check=health_check,
        )
    )
