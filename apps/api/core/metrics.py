"""Prometheus metrics: HTTP, Celery, provider calls, dead letters and queue depth."""

import hmac
import time
from collections.abc import Callable, Iterable
from http import HTTPStatus
from typing import cast

import redis
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests", ["method", "route", "status"], registry=REGISTRY
)
HTTP_LATENCY = Histogram(
    "http_request_seconds", "HTTP request latency", ["method", "route"], registry=REGISTRY
)
TASKS = Counter("celery_tasks_total", "Celery task outcomes", ["task", "state"], registry=REGISTRY)
PROVIDER_CALLS = Counter(
    "provider_calls_total", "Outbound provider calls", ["provider", "outcome"], registry=REGISTRY
)
DEAD_LETTERS = Counter(
    "dead_letters_total", "Tasks that exhausted retries", ["task"], registry=REGISTRY
)


def record_provider_call(provider: str, outcome: str) -> None:
    PROVIDER_CALLS.labels(provider=provider, outcome=outcome).inc()


class QueueDepthCollector(Collector):
    """Reads the length of every Celery queue from Redis at scrape time."""

    def describe(self) -> Iterable[Metric]:
        # Without this, register() calls collect() and hits Redis at import time.
        return []

    def collect(self) -> Iterable[Metric]:
        from config.celery import QUEUE_NAMES

        gauge = GaugeMetricFamily(
            "celery_queue_depth", "Messages waiting in a queue", labels=["queue"]
        )
        client = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2)
        try:
            for name in QUEUE_NAMES:
                gauge.add_metric([name], float(cast(int, client.llen(name))))
        except redis.RedisError:
            return []
        finally:
            client.close()
        return [gauge]


REGISTRY.register(QueueDepthCollector())


class MetricsMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        started = time.perf_counter()
        response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        route = (match.route if match and match.route else "unmatched")[:80]
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route).observe(time.perf_counter() - started)
        return response


def metrics_view(request: HttpRequest) -> HttpResponse:
    """Prometheus scrape endpoint, protected by a bearer token (open only when DEBUG is on)."""
    token = settings.METRICS_TOKEN
    if token:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            return HttpResponse(status=HTTPStatus.UNAUTHORIZED)
    elif not settings.DEBUG:
        return HttpResponse(status=HTTPStatus.NOT_FOUND)
    return HttpResponse(generate_latest(REGISTRY), content_type=CONTENT_TYPE_LATEST)
