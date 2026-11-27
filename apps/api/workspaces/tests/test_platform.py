"""Platform administration: only a superuser with MFA reaches it; tenants and owners are created safely."""

import json
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from audit.models import AuditEvent
from conftest import PASSWORD, make_user
from identity import mfa
from identity.models import RecoveryCode, TotpDevice, User
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


@pytest.fixture
def mfa_admin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The administrator has a second factor; everyone else is judged by their real devices."""
    real = mfa.mfa_enabled
    monkeypatch.setattr("identity.mfa.mfa_enabled", lambda user: user.is_superuser or real(user))


def add_second_factor(user: User) -> None:
    TotpDevice.objects.create(
        user=user,
        ciphertext=b"x",
        nonce=b"x",
        wrapped_key=b"x",
        wrapped_key_nonce=b"x",
        key_version="v1",
        confirmed_at=timezone.now(),
    )
    RecoveryCode.objects.create(user=user, code_hash="hash")


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


def test_members_are_listed_with_their_second_factor_status(
    admin: User, workspace: Workspace, mfa_admin_only: None
):
    owner = User.objects.get(email="owner@example.com")
    add_second_factor(owner)
    make_user("viewer@example.com", workspace, Role.VIEWER)
    rows = signed_in(admin).get(f"{TENANTS}/{workspace.pk}/members").json()
    by_email = {r["email"]: r for r in rows}
    assert by_email["owner@example.com"]["mfa_enabled"] is True
    assert by_email["owner@example.com"]["role"] == "owner"
    assert by_email["viewer@example.com"]["mfa_enabled"] is False


def test_resetting_mfa_removes_the_factors_signs_the_user_out_and_forces_enrolment(
    admin: User, workspace: Workspace, mfa_admin_only: None, settings
):
    settings.MFA_REQUIRED_ROLES = ["owner"]
    owner = User.objects.get(email="owner@example.com")
    owner_client = Client()
    owner_client.get(f"{BASE}/auth/csrf")
    login = call(
        owner_client, "post", f"{BASE}/auth/login", {"email": owner.email, "password": PASSWORD}
    )
    assert login.status_code == 200
    add_second_factor(owner)

    res = call(signed_in(admin), "post", f"{TENANTS}/{workspace.pk}/members/{owner.pk}/reset-mfa")
    assert res.status_code == 204
    assert not TotpDevice.objects.filter(user=owner).exists()
    assert not RecoveryCode.objects.filter(user=owner).exists()
    assert owner_client.get(f"{BASE}/auth/me").status_code == 401  # the old session is gone

    fresh = Client()
    fresh.get(f"{BASE}/auth/csrf")
    call(fresh, "post", f"{BASE}/auth/login", {"email": owner.email, "password": PASSWORD})
    assert fresh.get(f"{BASE}/auth/me").json()["mfa_enrollment_required"] is True
    event = AuditEvent.objects.get(action="mfa.reset_by_admin")
    assert event.actor_id == str(admin.pk) and event.target_id == str(owner.pk)
    assert AuditEvent.objects.filter(action="platform.member_mfa_reset").exists()


def test_a_member_of_another_tenant_cannot_be_reset_through_this_one(
    admin: User, workspace: Workspace, mfa_admin_only: None
):
    other = make_user("other@example.com")
    from workspaces.services import create_workspace

    create_workspace(name="Other Co", owner=other)
    add_second_factor(other)
    res = call(signed_in(admin), "post", f"{TENANTS}/{workspace.pk}/members/{other.pk}/reset-mfa")
    assert res.status_code == 404
    assert TotpDevice.objects.filter(user=other).exists()


def test_platform_administrators_cannot_be_reset_through_a_tenant(
    admin: User, workspace: Workspace, mfa_admin_only: None
):
    Membership.objects.create(workspace=workspace, user=admin, role=Role.ADMIN)
    res = call(signed_in(admin), "post", f"{TENANTS}/{workspace.pk}/members/{admin.pk}/reset-mfa")
    assert res.status_code == 404


def test_a_tenant_owner_cannot_use_the_member_endpoints(workspace: Workspace, mfa_done: None):
    owner = User.objects.get(email="owner@example.com")
    client = signed_in(owner)
    assert client.get(f"{TENANTS}/{workspace.pk}/members").status_code == 404
    assert (
        call(client, "post", f"{TENANTS}/{workspace.pk}/members/{owner.pk}/reset-mfa").status_code
        == 404
    )
