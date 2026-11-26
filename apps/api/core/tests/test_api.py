import json

import pytest
from django.test import Client

from audit.models import AuditEvent
from conftest import PASSWORD, make_user
from core import health
from identity.models import User
from workspaces.models import Workspace

pytestmark = pytest.mark.django_db
BASE = "/api/v1"


def _csrf(client: Client) -> str:
    return client.get(f"{BASE}/auth/csrf").json()["csrf_token"]


def _login(client: Client, email: str, password: str = PASSWORD, **extra: str):
    token = _csrf(client)
    return client.post(
        f"{BASE}/auth/login",
        data=json.dumps({"email": email, "password": password}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
        **extra,
    )


def test_problem_json_shape_and_request_id_header() -> None:
    client = Client()
    response = client.get(f"{BASE}/auth/me", HTTP_X_REQUEST_ID="req-123")
    assert response.status_code == 401
    assert response["Content-Type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "unauthenticated" and body["status"] == 401
    assert body["request_id"] == "req-123" and response["X-Request-Id"] == "req-123"


def test_login_requires_csrf_and_returns_memberships(owner: User, workspace: Workspace) -> None:
    client = Client(enforce_csrf_checks=True)
    denied = client.post(
        f"{BASE}/auth/login",
        data=json.dumps({"email": owner.email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert denied.status_code in {401, 403}

    ok = _login(client, "OWNER@example.com")
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "ok"
    assert body["me"]["email"] == "owner@example.com"
    assert body["me"]["memberships"][0]["workspace_id"] == str(workspace.pk)
    assert body["me"]["memberships"][0]["role"] == "owner"
    assert client.get(f"{BASE}/auth/me").status_code == 200


def test_bad_credentials_are_generic_and_audited(owner: User) -> None:
    client = Client(enforce_csrf_checks=True)
    response = _login(client, owner.email, "wrong-password-value")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert AuditEvent.objects.filter(action="auth.login_failed").exists()


def test_error_messages_come_from_the_catalog() -> None:
    client = Client()
    body = client.get(f"{BASE}/auth/me", HTTP_ACCEPT_LANGUAGE="en").json()
    assert body["detail"] == "Authentication is required."


def test_logout_ends_session(logged_in: Client) -> None:
    token = _csrf(logged_in)
    assert logged_in.post(f"{BASE}/auth/logout", HTTP_X_CSRFTOKEN=token).status_code == 204
    assert logged_in.get(f"{BASE}/auth/me").status_code == 401


def test_workspace_list_only_shows_own_workspaces(logged_in: Client, workspace: Workspace) -> None:
    other_owner = make_user("someone@example.com")
    from workspaces.services import create_workspace

    create_workspace(name="Hidden Co", owner=other_owner)
    names = [w["name"] for w in logged_in.get(f"{BASE}/workspaces/").json()]
    assert names == [workspace.name]


def test_liveness_and_readiness(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"]["database"] == "ok"

    def failing() -> None:
        raise ConnectionError("down")

    monkeypatch.setitem(health._checks, "redis", failing)
    degraded = client.get("/readyz")
    assert degraded.status_code == 503 and degraded.json()["checks"]["redis"].startswith("error")
