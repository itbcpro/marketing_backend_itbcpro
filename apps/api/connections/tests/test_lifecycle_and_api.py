import base64
import hashlib
import json
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from audit import outbox
from audit.actors import Actor
from audit.models import AuditEvent
from conftest import make_user
from connections import lifecycle, oauth, ratelimit, services
from connections.models import Connection, ConnectionStatus, Credential, CredentialKind
from connections.providers import (
    AuthType,
    FormField,
    HealthResult,
    OAuthConfig,
    ProviderDescriptor,
    UnknownProviderError,
    descriptor_as_dict,
    get_provider,
    missing_scopes,
    register_provider,
    unregister_provider,
)
from connections.tasks import check_health, refresh_tokens
from core.errors import (
    AuthExpiredError,
    PermanentProviderError,
    RateLimitedError,
    RetryableProviderError,
)
from core.locks import redis_lock
from workspaces.models import Role, Workspace

pytestmark = pytest.mark.django_db
BASE = "/api/v1/connections"
TOKEN_URL = "https://auth.example.test/token"
SYSTEM = Actor.system("test")


# ---- rate limiter -----------------------------------------------------------------------------


def test_token_bucket_allows_a_burst_then_denies_with_a_wait_hint() -> None:
    cid = uuid.uuid4()
    spec = ratelimit.BucketSpec(capacity=3, refill_per_second=1.0)
    now = 1_000_000
    assert all(ratelimit.acquire(cid, "api", spec, now_ms=now).allowed for _ in range(3))
    denied = ratelimit.acquire(cid, "api", spec, now_ms=now)
    assert denied.allowed is False and 0 < denied.retry_after_seconds <= 1.0
    refilled = ratelimit.acquire(cid, "api", spec, now_ms=now + 1500)
    assert refilled.allowed is True


def test_buckets_are_isolated_and_penalties_block_until_they_expire() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    spec = ratelimit.BucketSpec(capacity=1, refill_per_second=0.001)
    now = 5_000_000
    assert ratelimit.acquire(a, "x", spec, now_ms=now).allowed
    assert ratelimit.acquire(a, "y", spec, now_ms=now).allowed  # other bucket
    assert ratelimit.acquire(b, "x", spec, now_ms=now).allowed  # other connection

    ratelimit.penalize(b, "z", seconds=30, now_ms=now)
    blocked = ratelimit.acquire(b, "z", spec, now_ms=now + 1000)
    assert blocked.allowed is False and 28 < blocked.retry_after_seconds <= 29
    assert ratelimit.snapshot(b, "z")["blocked_until_ms"] == now + 30_000


def test_cost_larger_than_available_tokens_is_denied() -> None:
    cid = uuid.uuid4()
    spec = ratelimit.BucketSpec(capacity=5, refill_per_second=1.0)
    assert ratelimit.acquire(cid, "bulk", spec, cost=5, now_ms=10).allowed
    assert not ratelimit.acquire(cid, "bulk", spec, cost=2, now_ms=10).allowed


# ---- fake providers ---------------------------------------------------------------------------


@pytest.fixture
def api_key_provider():
    state = {"health": HealthResult(ConnectionStatus.ACTIVE)}
    register_provider(
        ProviderDescriptor(
            name="fakekey",
            display_name="Fake key provider",
            auth_type=AuthType.API_KEY,
            fields=(
                FormField("api_key", "API key", secret=True, pattern=r"[A-Za-z0-9]{8,}"),
                FormField("note", "Note", required=False),
            ),
            health_check=lambda c: state["health"],
            required_scopes=("read",),
        )
    )
    yield state
    unregister_provider("fakekey")


@pytest.fixture
def oauth_provider():
    register_provider(
        ProviderDescriptor(
            name="fakeoauth",
            display_name="Fake OAuth",
            auth_type=AuthType.OAUTH,
            oauth=lambda: OAuthConfig(
                authorize_url="https://auth.example.test/authorize",
                token_url=TOKEN_URL,
                client_id="client-1",
                client_secret="secret-1",
                scopes=("read", "write"),
                revoke_url="https://auth.example.test/revoke",
            ),
            identify=lambda token: ("acct-42", "Fake account"),
            required_scopes=("read",),
        )
    )
    yield
    unregister_provider("fakeoauth")


def _oauth_connection(
    workspace: Workspace, *, expires_in: timedelta, refresh: str | None = "r-1"
) -> Connection:
    connection = services.create_connection(
        workspace_id=workspace.pk,
        provider="fakeoauth",
        external_account_id=str(uuid.uuid4()),
        display_name="acct",
        actor=SYSTEM,
    )
    services.store_secret(connection, CredentialKind.ACCESS_TOKEN, "a-old", actor=SYSTEM)
    if refresh:
        services.store_secret(connection, CredentialKind.REFRESH_TOKEN, refresh, actor=SYSTEM)
    connection.expires_at = timezone.now() + expires_in
    connection.granted_scopes = ["read"]
    connection.status = ConnectionStatus.ACTIVE
    connection.save()
    return connection


# ---- registry and OAuth client ----------------------------------------------------------------


def test_registry_lookup_scope_gaps_and_form_descriptor(api_key_provider) -> None:
    descriptor = get_provider("fakekey")
    assert missing_scopes(descriptor, []) == ["read"] and missing_scopes(descriptor, ["read"]) == []
    described = descriptor_as_dict(descriptor)
    assert described["fields"][0] == {
        "name": "api_key",
        "label": "API key",
        "secret": True,
        "required": True,
        "pattern": "[A-Za-z0-9]{8,}",
        "help_text": "",
    }
    with pytest.raises(UnknownProviderError):
        get_provider("does-not-exist")


def test_authorization_url_carries_state_and_a_valid_pkce_challenge(oauth_provider) -> None:
    config = get_provider("fakeoauth").oauth()
    request = oauth.build_authorization_request(config, redirect_uri="https://app.test/cb")
    query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
    assert query["state"] == request.state and query["client_id"] == "client-1"
    assert query["scope"] == "read write" and query["code_challenge_method"] == "S256"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(request.code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == expected
    assert oauth.build_authorization_request(config, redirect_uri="x").state != request.state


@respx.mock
def test_code_exchange_and_refresh_send_the_expected_form(oauth_provider) -> None:
    config = get_provider("fakeoauth").oauth()
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "a1",
                "refresh_token": "r1",
                "expires_in": 3600,
                "scope": "read,write",
            },
        )
    )
    tokens = oauth.exchange_code(
        config, provider="fakeoauth", code="c", redirect_uri="u", code_verifier="v"
    )
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["grant_type"] == ["authorization_code"] and sent["code_verifier"] == ["v"]
    assert tokens.scopes == ["read", "write"] and tokens.refresh_token == "r1"
    assert tokens.expires_at and tokens.expires_at > timezone.now() + timedelta(minutes=59)

    oauth.refresh(config, provider="fakeoauth", refresh_token="r1")
    assert parse_qs(route.calls.last.request.content.decode())["grant_type"] == ["refresh_token"]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), AuthExpiredError),
        (httpx.Response(400, json={"error": "invalid_request"}), PermanentProviderError),
        (httpx.Response(429), RateLimitedError),
        (httpx.Response(503), RetryableProviderError),
        (httpx.Response(200, text="not json"), PermanentProviderError),
    ],
)
@respx.mock
def test_token_endpoint_errors_map_to_the_provider_taxonomy(
    oauth_provider, response, expected
) -> None:
    respx.post(TOKEN_URL).mock(return_value=response)
    with pytest.raises(expected):
        oauth.refresh(get_provider("fakeoauth").oauth(), provider="fakeoauth", refresh_token="r")


@respx.mock
def test_network_failures_are_retryable(oauth_provider) -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("offline"))
    with pytest.raises(RetryableProviderError):
        oauth.refresh(get_provider("fakeoauth").oauth(), provider="fakeoauth", refresh_token="r")


# ---- refresh lifecycle ------------------------------------------------------------------------


@respx.mock
def test_refresh_replaces_tokens_and_reactivates(workspace: Workspace, oauth_provider) -> None:
    connection = _oauth_connection(workspace, expires_in=timedelta(minutes=2))
    Connection.objects.filter(pk=connection.pk).update(status=ConnectionStatus.DEGRADED)
    connection.refresh_from_db()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a-new", "expires_in": 3600})
    )
    assert lifecycle.refresh_connection(connection) is True
    connection.refresh_from_db()
    assert connection.status == ConnectionStatus.ACTIVE
    assert services.read_secret(connection, CredentialKind.ACCESS_TOKEN) == "a-new"
    assert services.read_secret(connection, CredentialKind.REFRESH_TOKEN) == "r-1"  # kept
    assert connection.expires_at and connection.expires_at > timezone.now() + timedelta(minutes=50)


@respx.mock
def test_refresh_is_single_flight(workspace: Workspace, oauth_provider) -> None:
    connection = _oauth_connection(workspace, expires_in=timedelta(minutes=2))
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "x"}))
    with redis_lock(f"refresh:{connection.pk}", ttl_seconds=30):
        assert lifecycle.refresh_connection(connection) is False
    assert route.call_count == 0


@respx.mock
def test_revoked_authorisation_marks_the_connection_expired_and_emits_an_event(
    workspace: Workspace, oauth_provider
) -> None:
    connection = _oauth_connection(workspace, expires_in=timedelta(minutes=2))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    seen: list[dict] = []
    outbox.register_handler("connection.status_changed", seen.append)

    assert lifecycle.refresh_connection(connection) is False
    connection.refresh_from_db()
    assert connection.status == ConnectionStatus.EXPIRED
    assert AuditEvent.objects.filter(
        action="connection.status_changed", target_id=str(connection.pk)
    ).exists()
    outbox.dispatch_batch()
    assert any(e["connection_id"] == str(connection.pk) and e["to"] == "expired" for e in seen)


@respx.mock
def test_only_due_connections_are_refreshed(workspace: Workspace, oauth_provider) -> None:
    due = _oauth_connection(workspace, expires_in=timedelta(minutes=5))
    later = _oauth_connection(workspace, expires_in=timedelta(hours=5))
    no_refresh = _oauth_connection(workspace, expires_in=timedelta(minutes=5), refresh=None)
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "n", "expires_in": 3600})
    )
    assert lifecycle.refresh_due_connections() == 1
    assert route.call_count == 1
    for untouched in (later, no_refresh):
        untouched.refresh_from_db()
        assert services.read_secret(untouched, CredentialKind.ACCESS_TOKEN) == "a-old"
    due.refresh_from_db()
    assert services.read_secret(due, CredentialKind.ACCESS_TOKEN) == "n"


@respx.mock
def test_scheduled_tasks_run_end_to_end(workspace: Workspace, oauth_provider) -> None:
    _oauth_connection(workspace, expires_in=timedelta(minutes=1))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "n", "expires_in": 3600})
    )
    assert refresh_tokens.apply(throw=True).get() == 1
    result = check_health.apply(throw=True).get()
    assert result == {"active": 1}


# ---- health checks ----------------------------------------------------------------------------


def _key_connection(workspace: Workspace) -> Connection:
    connection = services.create_connection(
        workspace_id=workspace.pk,
        provider="fakekey",
        external_account_id="k",
        display_name="k",
        actor=SYSTEM,
    )
    connection.granted_scopes = ["read"]
    connection.save()
    return connection


def test_health_check_applies_provider_result_scope_gaps_and_expiry(
    workspace: Workspace, api_key_provider
) -> None:
    connection = _key_connection(workspace)
    assert lifecycle.check_connection(connection).status == ConnectionStatus.ACTIVE

    api_key_provider["health"] = HealthResult(ConnectionStatus.ERROR, "boom")
    result = lifecycle.check_connection(connection)
    connection.refresh_from_db()
    assert result.status == ConnectionStatus.ERROR and connection.health_detail == "boom"

    api_key_provider["health"] = HealthResult(ConnectionStatus.ACTIVE)
    connection.granted_scopes = []
    connection.save()
    gap = lifecycle.check_connection(connection)
    assert gap.status == ConnectionStatus.DEGRADED and "read" in gap.detail

    connection.granted_scopes = ["read"]
    connection.expires_at = timezone.now() + timedelta(days=2)
    connection.save()
    assert lifecycle.check_connection(connection).detail == "token expires soon"
    connection.expires_at = timezone.now() - timedelta(minutes=1)
    connection.save()
    assert lifecycle.check_connection(connection).status == ConnectionStatus.EXPIRED


def test_health_check_translates_provider_exceptions(
    workspace: Workspace, api_key_provider
) -> None:
    connection = _key_connection(workspace)

    def raise_expired(_: Connection) -> HealthResult:
        raise AuthExpiredError("gone", provider="fakekey")

    unregister_provider("fakekey")
    register_provider(
        ProviderDescriptor(
            name="fakekey", display_name="x", auth_type=AuthType.API_KEY, health_check=raise_expired
        )
    )
    assert lifecycle.check_connection(connection).status == ConnectionStatus.EXPIRED


def test_unregistered_providers_are_reported_as_errors(workspace: Workspace) -> None:
    connection = services.create_connection(
        workspace_id=workspace.pk,
        provider="ghost",
        external_account_id="g",
        display_name="g",
        actor=SYSTEM,
    )
    assert lifecycle.check_connection(connection).status == ConnectionStatus.ERROR
    assert lifecycle.check_all_connections() == {"error": 1}


# ---- API --------------------------------------------------------------------------------------


def _headers(workspace: Workspace) -> dict[str, str]:
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def _post(client: Client, path: str, body: dict, workspace: Workspace):
    return client.post(
        f"{BASE}{path}",
        data=json.dumps(body),
        content_type="application/json",
        **_headers(workspace),
    )


def test_create_list_test_and_delete_a_key_based_connection(
    logged_in: Client, workspace: Workspace, api_key_provider
) -> None:
    listing = logged_in.get(f"{BASE}/providers", **_headers(workspace)).json()
    assert "fakekey" in [p["name"] for p in listing]

    created = _post(
        logged_in,
        "/",
        {
            "provider": "fakekey",
            "external_account_id": "acct-1",
            "display_name": "Main",
            "fields": {"api_key": "AbCd1234Xy"},
        },
        workspace,
    )
    assert created.status_code == 200, created.content
    body = created.json()
    # No scopes were reported but the provider requires "read": surfaced as a permission gap.
    assert body["status"] == "degraded" and body["missing_scopes"] == ["read"]
    assert "AbCd1234Xy" not in json.dumps(body)
    stored = Credential.objects.get(connection_id=body["id"])
    assert b"AbCd1234Xy" not in bytes(stored.ciphertext)
    assert (
        services.read_secret(Connection.objects.get(pk=body["id"]), CredentialKind.API_KEY)
        == "AbCd1234Xy"
    )

    api_key_provider["health"] = HealthResult(ConnectionStatus.DEGRADED, "slow")
    tested = _post(logged_in, f"/{body['id']}/test", {}, workspace).json()
    assert tested["status"] == "degraded" and tested["health_detail"] == "slow"

    listed = logged_in.get(f"{BASE}/", **_headers(workspace)).json()
    assert [c["id"] for c in listed] == [body["id"]]

    deleted = logged_in.delete(f"{BASE}/{body['id']}", **_headers(workspace))
    assert deleted.status_code == 204
    assert not Credential.objects.filter(connection_id=body["id"]).exists()
    assert Connection.objects.get(pk=body["id"]).status == ConnectionStatus.REVOKED
    assert AuditEvent.objects.filter(action="credential.destroyed").exists()


@pytest.mark.parametrize("fields", [{}, {"api_key": "short"}, {"api_key": "has spaces in it!"}])
def test_create_validates_fields_against_the_descriptor(
    logged_in: Client, workspace: Workspace, api_key_provider, fields
) -> None:
    response = _post(
        logged_in,
        "/",
        {"provider": "fakekey", "external_account_id": "a", "fields": fields},
        workspace,
    )
    assert response.status_code == 422
    assert not Connection.objects.exists()


def test_unknown_and_oauth_providers_cannot_use_the_key_form(
    logged_in: Client, workspace: Workspace, oauth_provider
) -> None:
    assert (
        _post(
            logged_in,
            "/",
            {"provider": "nope", "external_account_id": "a", "fields": {}},
            workspace,
        ).status_code
        == 404
    )
    assert (
        _post(
            logged_in,
            "/",
            {"provider": "fakeoauth", "external_account_id": "a", "fields": {}},
            workspace,
        ).status_code
        == 422
    )


def test_managing_connections_requires_the_permission(
    workspace: Workspace, api_key_provider
) -> None:
    editor = make_user("editor@example.com", workspace, Role.EDITOR)
    client = Client()
    client.force_login(editor)
    body = {"provider": "fakekey", "external_account_id": "a", "fields": {"api_key": "AbCd1234Xy"}}
    assert _post(client, "/", body, workspace).status_code == 403
    assert client.get(f"{BASE}/", **_headers(workspace)).status_code == 200  # read is allowed


def test_connections_are_invisible_across_workspaces(
    logged_in: Client, workspace: Workspace, owner, api_key_provider
) -> None:
    from workspaces.services import create_workspace

    other = create_workspace(name="Other", owner=owner)
    foreign = services.create_connection(
        workspace_id=other.pk,
        provider="fakekey",
        external_account_id="f",
        display_name="f",
        actor=SYSTEM,
    )
    assert logged_in.get(f"{BASE}/", **_headers(workspace)).json() == []
    assert _post(logged_in, f"/{foreign.pk}/test", {}, workspace).status_code == 404
    assert logged_in.delete(f"{BASE}/{foreign.pk}", **_headers(workspace)).status_code == 404


@respx.mock
def test_oauth_start_and_callback_create_a_connection(
    logged_in: Client, workspace: Workspace, owner, oauth_provider
) -> None:
    started = _post(logged_in, "/oauth/fakeoauth/start", {}, workspace)
    assert started.status_code == 200
    url = started.json()["authorization_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert cache.get(f"oauth-state:{state}")["workspace_id"] == str(workspace.pk)

    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "acc",
                "refresh_token": "ref",
                "expires_in": 3600,
                "scope": "read",
            },
        )
    )
    callback = logged_in.get(
        f"{BASE}/oauth/fakeoauth/callback", {"code": "the-code", "state": state}
    )
    assert callback.status_code == 302 and "connected=fakeoauth" in callback["Location"]
    connection = Connection.objects.get(provider="fakeoauth")
    assert (
        connection.external_account_id == "acct-42" and connection.status == ConnectionStatus.ACTIVE
    )
    assert services.read_secret(connection, CredentialKind.REFRESH_TOKEN) == "ref"

    replay = logged_in.get(f"{BASE}/oauth/fakeoauth/callback", {"code": "the-code", "state": state})
    assert replay.status_code == 422  # state is single use


def test_oauth_callback_rejects_foreign_or_unknown_state(
    logged_in: Client, workspace: Workspace, oauth_provider
) -> None:
    assert (
        logged_in.get(
            f"{BASE}/oauth/fakeoauth/callback", {"code": "c", "state": "made-up"}
        ).status_code
        == 422
    )
    state = parse_qs(
        urlsplit(
            _post(logged_in, "/oauth/fakeoauth/start", {}, workspace).json()["authorization_url"]
        ).query
    )["state"][0]
    intruder = make_user("intruder@example.com", workspace, Role.ADMIN)
    other = Client()
    other.force_login(intruder)
    assert (
        other.get(f"{BASE}/oauth/fakeoauth/callback", {"code": "c", "state": state}).status_code
        == 422
    )
    assert not Connection.objects.filter(provider="fakeoauth").exists()
