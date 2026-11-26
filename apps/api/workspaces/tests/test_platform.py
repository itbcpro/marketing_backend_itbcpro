"""Platform administration: only a superuser with MFA reaches it; tenants and owners are created safely."""

import json
from typing import Any

import pytest
from django.test import Client

from audit.models import AuditEvent
from conftest import PASSWORD, make_user
from identity.models import User
from workspaces.models import Membership, Role, Workspace

pytestmark = pytest.mark.django_db
BASE = "/api/v1"
TENANTS = f"{BASE}/platform/tenants"
NEW_TENANT = {
    "organization_name": "Acme Roofing",
    "owner_email": "Boss@Acme.example",
    "owner_full_name": "The Boss",
}


@pytest.fixture
def admin(db: None) -> User:
    return User.objects.create_superuser("root@example.com", PASSWORD)


@pytest.fixture
def mfa_done(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("identity.mfa.mfa_enabled", lambda user: True)


def signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    client.get(f"{BASE}/auth/csrf")
    return client


def call(client: Client, method: str, path: str, body: dict[str, Any] | None = None):
    return getattr(client, method)(
        path, data=json.dumps(body or {}), content_type="application/json"
    )


def test_a_tenant_owner_cannot_see_the_platform_endpoints(workspace: Workspace, mfa_done: None):
    client = signed_in(User.objects.get(email="owner@example.com"))
    assert client.get(TENANTS).status_code == 404
    assert call(client, "post", TENANTS, NEW_TENANT).status_code == 404
    assert client.get(f"{TENANTS}/{workspace.pk}").status_code == 404
    assert (
        call(client, "patch", f"{TENANTS}/{workspace.pk}", {"is_active": False}).status_code == 404
    )
    assert (
        call(client, "post", f"{TENANTS}/{workspace.pk}/owners", {"email": "x@y.io"}).status_code
        == 404
    )
    assert Workspace.objects.get(pk=workspace.pk).is_active


def test_anonymous_callers_and_bearer_tokens_are_refused(admin: User):
    assert Client().get(TENANTS).status_code == 401
    assert Client().get(TENANTS, HTTP_AUTHORIZATION="Bearer not-a-token").status_code == 401


def test_a_platform_admin_without_mfa_is_blocked_until_it_is_set_up(admin: User):
    res = signed_in(admin).get(TENANTS)
    assert res.status_code == 403
    assert "mfa_required" in res.content.decode()


def test_me_tells_the_front_end_who_is_a_platform_admin(admin: User, workspace: Workspace):
    assert signed_in(admin).get(f"{BASE}/auth/me").json()["is_platform_admin"] is True
    owner = User.objects.get(email="owner@example.com")
    assert signed_in(owner).get(f"{BASE}/auth/me").json()["is_platform_admin"] is False


def test_creating_a_tenant_makes_the_organization_workspace_and_owner(admin: User, mfa_done: None):
    res = call(signed_in(admin), "post", TENANTS, NEW_TENANT)
    assert res.status_code == 201
    body = res.json()
    assert body["tenant"]["name"] == "Acme Roofing"
    assert body["tenant"]["owner_emails"] == ["boss@acme.example"]
    assert body["owner"]["existing_user"] is False
    password = body["owner"]["temporary_password"]
    assert password

    owner = User.objects.get(email="boss@acme.example")
    assert owner.must_change_password and owner.check_password(password)
    workspace = Workspace.objects.get(name="Acme Roofing")
    assert Membership.objects.get(workspace=workspace, user=owner).role == Role.OWNER
    assert workspace.organization.name == "Acme Roofing"


def test_the_password_never_reaches_the_audit_log_and_the_event_names_the_admin(
    admin: User, mfa_done: None
):
    password = call(signed_in(admin), "post", TENANTS, NEW_TENANT).json()["owner"][
        "temporary_password"
    ]
    event = AuditEvent.objects.get(action="platform.tenant_created")
    assert event.actor_id == str(admin.pk)
    assert password not in json.dumps(event.after)


def test_the_new_owner_signs_in_must_change_the_password_and_then_is_free(
    admin: User, mfa_done: None, monkeypatch: pytest.MonkeyPatch
):
    password = call(signed_in(admin), "post", TENANTS, NEW_TENANT).json()["owner"][
        "temporary_password"
    ]
    monkeypatch.undo()  # the new owner has no second factor yet
    client = Client()
    client.get(f"{BASE}/auth/csrf")
    login = call(
        client, "post", f"{BASE}/auth/login", {"email": "boss@acme.example", "password": password}
    )
    assert login.status_code == 200
    assert client.get(f"{BASE}/auth/me").json()["must_change_password"] is True

    changed = call(
        client,
        "post",
        f"{BASE}/auth/password/change",
        {"current_password": password, "new_password": "another-long-unusual-pass-7731"},
    )
    assert changed.status_code == 204
    assert client.get(f"{BASE}/auth/me").json()["must_change_password"] is False


def test_an_existing_account_becomes_an_owner_without_a_new_password(
    admin: User, workspace: Workspace, mfa_done: None
):
    body = {**NEW_TENANT, "owner_email": "owner@example.com"}
    res = call(signed_in(admin), "post", TENANTS, body)
    assert res.status_code == 201
    assert res.json()["owner"] == {
        "email": "owner@example.com",
        "existing_user": True,
        "temporary_password": None,
    }
    owner = User.objects.get(email="owner@example.com")
    assert not owner.must_change_password and owner.check_password(PASSWORD)
    assert Membership.objects.filter(user=owner, role=Role.OWNER).count() == 2


def test_a_taken_slug_is_a_conflict_and_nothing_is_created(
    admin: User, workspace: Workspace, mfa_done: None
):
    res = call(signed_in(admin), "post", TENANTS, {**NEW_TENANT, "slug": workspace.slug})
    assert res.status_code == 409
    assert not User.objects.filter(email="boss@acme.example").exists()
    assert not Workspace.objects.filter(name="Acme Roofing").exists()


@pytest.mark.parametrize(
    ("field", "value"), [("owner_email", "not-an-email"), ("organization_name", "  ")]
)
def test_invalid_input_is_rejected(admin: User, mfa_done: None, field: str, value: str):
    res = call(signed_in(admin), "post", TENANTS, {**NEW_TENANT, field: value})
    assert res.status_code in (400, 422)
    assert not Workspace.objects.filter(name="Acme Roofing").exists()


def test_the_list_shows_every_tenant_with_its_owner(
    admin: User, workspace: Workspace, mfa_done: None
):
    rows = signed_in(admin).get(TENANTS).json()
    row = next(r for r in rows if r["id"] == str(workspace.pk))
    assert row["owner_emails"] == ["owner@example.com"] and row["member_count"] == 1
    assert row["is_active"] is True


def test_deactivating_a_tenant_locks_its_members_out_and_it_can_come_back(
    admin: User, workspace: Workspace, mfa_done: None
):
    owner = User.objects.get(email="owner@example.com")
    client = signed_in(admin)
    off = call(client, "patch", f"{TENANTS}/{workspace.pk}", {"is_active": False})
    assert off.status_code == 200 and off.json()["is_active"] is False
    assert signed_in(owner).get(f"{BASE}/auth/me").json()["memberships"] == []
    assert AuditEvent.objects.filter(action="platform.tenant_deactivated").exists()

    on = call(client, "patch", f"{TENANTS}/{workspace.pk}", {"is_active": True})
    assert on.json()["is_active"] is True
    assert len(signed_in(owner).get(f"{BASE}/auth/me").json()["memberships"]) == 1


def test_adding_an_owner_to_a_tenant(admin: User, workspace: Workspace, mfa_done: None):
    colleague = make_user("colleague@example.com")
    res = call(
        signed_in(admin), "post", f"{TENANTS}/{workspace.pk}/owners", {"email": colleague.email}
    )
    assert res.status_code == 201 and res.json()["existing_user"] is True
    assert Membership.objects.get(workspace=workspace, user=colleague).role == Role.OWNER
    assert AuditEvent.objects.filter(action="platform.owner_added").exists()


def test_an_unknown_tenant_is_not_found(admin: User, mfa_done: None):
    missing = "00000000-0000-0000-0000-000000000001"
    client = signed_in(admin)
    assert client.get(f"{TENANTS}/{missing}").status_code == 404
    assert call(client, "patch", f"{TENANTS}/{missing}", {"is_active": False}).status_code == 404
