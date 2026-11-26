"""Error reporting (Sentry) and distributed tracing (OpenTelemetry) setup."""

from typing import Any

import sentry_sdk
from django.conf import settings
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from core.logging import scrub_mapping

_tracing_configured = False


def scrub_sentry_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Remove secrets and request bodies from an event before it leaves the process."""
    cleaned: dict[str, Any] = scrub_mapping(event)
    request = cleaned.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                k: v for k, v in headers.items() if k.lower() not in {"cookie", "authorization"}
            }
    return cleaned


def init_sentry() -> bool:
    dsn = getattr(settings, "SENTRY_DSN", "")
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=getattr(settings, "APP_ENVIRONMENT", "dev"),
        send_default_pii=False,
        traces_sample_rate=0.0,
        before_send=scrub_sentry_event,  # type: ignore[arg-type]
    )
    return True


def configure_tracing(exporter: SpanExporter | None = None) -> bool:
    """Enable tracing when an OTLP endpoint (or an explicit exporter) is provided."""
    global _tracing_configured  # noqa: PLW0603
    endpoint = getattr(settings, "OTEL_ENDPOINT", "")
    if _tracing_configured or not (endpoint or exporter):
        return _tracing_configured
    provider = TracerProvider(
        resource=Resource.create({"service.name": getattr(settings, "OTEL_SERVICE_NAME", "api")})
    )
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    DjangoInstrumentor().instrument()
    PsycopgInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]
    _tracing_configured = True
    return True
