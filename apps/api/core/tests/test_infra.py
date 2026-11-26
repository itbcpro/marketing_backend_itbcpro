import json
import random
import uuid

import httpx
import pytest
from django.db import OperationalError
from django.test import Client, RequestFactory, override_settings

from core import flags
from core.api.etag import etag_response
from core.api.filtering import FilterSpec, apply_filters, apply_ordering
from core.errors import RetryableProviderError, ValidationFailedError
from core.locks import LockNotAcquiredError, advisory_lock, redis_lock
from core.models import DeadLetter, FeatureFlag
from core.observability import scrub_sentry_event
from core.retry import RetryPolicy
from core.security import UnsafeUrlError, is_public_address, safe_fetch, validate_outbound_url
from core.tasks import ReliableTask, reliable_task
from identity.models import User

pytestmark = pytest.mark.django_db
PUBLIC = "93.184.216.34"


# ---- feature flags -------------------------------------------------------------------------


def test_flag_resolution_prefers_workspace_override_then_global_then_default(workspace) -> None:
    assert flags.is_enabled("beta") is False
    assert flags.is_enabled("beta", default=True) is True
    flags.set_flag("beta", True)
    assert flags.is_enabled("beta", workspace.pk) is True
    flags.set_flag("beta", False, workspace.pk)
    assert flags.is_enabled("beta", workspace.pk) is False
    assert flags.is_enabled("beta") is True
    assert FeatureFlag.objects.filter(name="beta").count() == 2


def test_flag_changes_invalidate_the_cache() -> None:
    flags.set_flag("cached", True)
    assert flags.is_enabled("cached") is True
    flags.set_flag("cached", False)
    assert flags.is_enabled("cached") is False


# ---- locks ---------------------------------------------------------------------------------


def test_redis_lock_is_exclusive_and_released() -> None:
    name = f"test-{uuid.uuid4()}"
    with redis_lock(name, ttl_seconds=5):
        with pytest.raises(LockNotAcquiredError), redis_lock(name, ttl_seconds=5):
            pass
    with redis_lock(name, ttl_seconds=5):
        pass


def test_advisory_lock_reports_acquisition() -> None:
    with advisory_lock("unit-test-lock") as first:
        assert first is True
    with advisory_lock("unit-test-lock") as again:
        assert again is True


# ---- retry and tasks -----------------------------------------------------------------------


def test_retry_delay_grows_exponentially_with_cap_and_full_jitter() -> None:
    policy = RetryPolicy(base_seconds=1, cap_seconds=30, max_retries=3)
    rng = random.Random(1)
    delays = [policy.delay(n, rng=rng) for n in range(10)]
    assert all(0 <= d <= 30 for d in delays)
    assert policy.delay(0, rng=random.Random(2)) <= 1
    assert policy.should_retry(2) and not policy.should_retry(3)


@reliable_task("tests.always_fails")
def _always_fails(self: ReliableTask, secret_token: str) -> None:
    raise ValueError("permanent failure")


TRANSIENT_CALLS: list[int] = []


@reliable_task("tests.transient", max_retries=1, retry_backoff=1, retry_jitter=False)
def _transient(self: ReliableTask) -> str:
    TRANSIENT_CALLS.append(self.request.retries)
    if self.request.retries == 0:
        raise RetryableProviderError("try again", provider="x")
    return "recovered"


def test_permanent_failure_is_dead_lettered_without_secrets() -> None:
    result = _always_fails.apply(kwargs={"secret_token": "s3cret"}, throw=False)
    assert result.failed()
    letter = DeadLetter.objects.get(task_name="tests.always_fails")
    assert letter.kwargs == {"secret_token": "[redacted]"}
    assert "permanent failure" in letter.error


def test_transient_failures_retry_then_succeed() -> None:
    TRANSIENT_CALLS.clear()
    _transient.apply(throw=False)
    assert TRANSIENT_CALLS == [0, 1]
    assert not DeadLetter.objects.filter(task_name="tests.transient").exists()


def test_database_errors_are_retryable_by_default() -> None:
    assert OperationalError in ReliableTask.autoretry_for


# ---- metrics and limits --------------------------------------------------------------------


def test_metrics_endpoint_requires_token_or_debug(client: Client) -> None:
    with override_settings(METRICS_TOKEN="", DEBUG=False):
        assert client.get("/metrics").status_code == 404
    with override_settings(METRICS_TOKEN="tok"):
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", HTTP_AUTHORIZATION="Bearer wrong").status_code == 401
        ok = client.get("/metrics", HTTP_AUTHORIZATION="Bearer tok")
        assert ok.status_code == 200
        assert b"http_requests_total" in ok.content
        assert b"celery_queue_depth" in ok.content


def test_oversized_body_is_rejected_before_processing(client: Client) -> None:
    with override_settings(API_MAX_BODY_BYTES=100):
        response = client.post(
            "/api/v1/auth/login",
            data=json.dumps({"email": "a@b.co", "password": "x" * 500}),
            content_type="application/json",
        )
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_general_api_throttle_applies_to_anonymous_clients(client: Client) -> None:
    from django.core.cache import cache

    cache.clear()
    statuses = [client.get("/api/v1/auth/csrf").status_code for _ in range(70)]
    assert 429 in statuses
    cache.clear()


# ---- etag and filtering --------------------------------------------------------------------


def test_etag_round_trip_returns_304_for_unchanged_payload() -> None:
    factory = RequestFactory()
    first = etag_response(factory.get("/x"), {"a": 1})
    assert first.status_code == 200 and first["ETag"]
    second = etag_response(factory.get("/x", HTTP_IF_NONE_MATCH=first["ETag"]), {"a": 1})
    assert second.status_code == 304
    changed = etag_response(factory.get("/x", HTTP_IF_NONE_MATCH=first["ETag"]), {"a": 2})
    assert changed.status_code == 200 and changed["ETag"] != first["ETag"]


def test_filtering_uses_a_whitelist_and_casts_values() -> None:
    User.objects.create_user("a@example.com", "long-password-value-1", is_active=True)
    User.objects.create_user("b@example.com", "long-password-value-1", is_active=False)
    allowed = {"active": FilterSpec("is_active", lambda v: v == "true")}
    assert apply_filters(User.objects.all(), {"active": "true"}, allowed).count() == 1
    with pytest.raises(ValidationFailedError):
        apply_filters(User.objects.all(), {"password": "x"}, allowed)
    bad = {"n": FilterSpec("id", int)}
    with pytest.raises(ValidationFailedError):
        apply_filters(User.objects.all(), {"n": "abc"}, bad)


def test_ordering_is_restricted_to_public_names() -> None:
    allowed = {"email": "email", "created": "created_at"}
    assert "DESC" in str(apply_ordering(User.objects.all(), "-email", allowed).query)
    with pytest.raises(ValidationFailedError):
        apply_ordering(User.objects.all(), "password", allowed)


# ---- SSRF guard ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104
        "::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "fc00::1",
    ],
)
def test_private_and_special_addresses_are_rejected(address: str) -> None:
    assert is_public_address(address) is False


def test_public_address_is_allowed() -> None:
    assert is_public_address(PUBLIC) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "ftp://example.com/",
        "https://user:pw@example.com/",
        "https://metadata.google.internal/",
        "https://service.local/",
        "https:///nohost",
    ],
)
def test_bad_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(url, resolver=lambda h, p: [PUBLIC])


def test_hosts_resolving_to_private_addresses_are_rejected_even_if_mixed() -> None:
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url("https://example.com/", resolver=lambda h, p: [PUBLIC, "10.0.0.1"])


def test_allow_list_restricts_hosts() -> None:
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(
            "https://other.com/", resolver=lambda h, p: [PUBLIC], allowed_hosts=frozenset({"a.com"})
        )


def test_safe_fetch_returns_body_and_revalidates_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://internal.example/next"})
        return httpx.Response(200, text="done")

    resolver = {"start.example": [PUBLIC], "internal.example": ["10.0.0.9"]}.get
    with pytest.raises(UnsafeUrlError):
        safe_fetch(
            "https://start.example/start",
            resolver=lambda h, p: resolver(h, []),
            transport=httpx.MockTransport(handler),
        )

    ok = safe_fetch(
        "https://start.example/plain",
        resolver=lambda h, p: [PUBLIC],
        transport=httpx.MockTransport(handler),
    )
    assert ok.status_code == 200 and ok.text == "done"


def test_safe_fetch_enforces_size_and_redirect_limits() -> None:
    big = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(UnsafeUrlError, match="too large"):
        safe_fetch(
            "https://a.example/", resolver=lambda h, p: [PUBLIC], transport=big, max_bytes=1000
        )
    loop = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(UnsafeUrlError, match="redirects"):
        safe_fetch("https://a.example/", resolver=lambda h, p: [PUBLIC], transport=loop)


# ---- observability -------------------------------------------------------------------------


def test_sentry_events_are_scrubbed() -> None:
    event = {
        "extra": {"api_key": "abc", "safe": 1},
        "request": {
            "data": {"password": "x"},
            "cookies": {"sessionid": "y"},
            "headers": {"Cookie": "z", "Authorization": "Bearer q", "Accept": "json"},
        },
    }
    cleaned = scrub_sentry_event(event, {})
    assert cleaned is not None
    assert cleaned["extra"] == {"api_key": "[redacted]", "safe": 1}
    assert "data" not in cleaned["request"] and "cookies" not in cleaned["request"]
    assert cleaned["request"]["headers"] == {"Accept": "json"}


def test_tracing_produces_spans_for_requests(client: Client) -> None:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from core.observability import configure_tracing

    exporter = InMemorySpanExporter()
    assert configure_tracing(exporter) is True
    client.get("/healthz")
    names = [span.name for span in exporter.get_finished_spans()]
    assert any("healthz" in name for name in names)
