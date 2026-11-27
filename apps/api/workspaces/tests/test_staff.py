"""Staff management: roles are handed out safely and never across tenants."""

import json
from typing import Any

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone

from audit.models import AuditEvent
from conftest import PASSWORD, make_user
from identity.models import RecoveryCode, TotpDevice, User
from workspaces.models import Membership, Role, Workspace
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
STAFF = "/api/v1/staff/"


def client_for(user: User, workspace: Workspace) -> Client:
    client = Client(HTTP_X_WORKSPACE_ID=str(workspace.pk))
    client.force_login(user)
    client.get("/api/v1/auth/csrf")
    return client


def send(client: Client, method: str, path: str, body: dict[str, Any] | None = None):
    return getattr(client, method)(
        path, data=json.dumps(body or {}), content_type="application/json"
    )


@pytest.fixture
def owner_user(workspace: Workspace) -> User:
    return User.objects.get(email="owner@example.com")


@pytest.fixture
def admin_user(workspace: Workspace) -> User:
    return make_user("admin@example.com", workspace, Role.ADMIN)


@pytest.fixture
def editor_user(workspace: Workspace) -> User:
    return make_user("editor@example.com", workspace, Role.EDITOR)


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


# ---- who may use it ---------------------------------------------------------------------------


def test_owners_and_admins_see_the_staff_and_everyone_else_is_refused(
    workspace: Workspace, owner_user: User, admin_user: User, editor_user: User
):
    for user in (owner_user, admin_user):
        rows = client_for(user, workspace).get(STAFF).json()
        assert {r["email"] for r in rows} == {
            "owner@example.com",
            "admin@example.com",
            "editor@example.com",
        }
    assert client_for(editor_user, workspace).get(STAFF).status_code == 403
    assert Client().get(STAFF).status_code == 401


def test_the_role_catalogue_shows_only_what_the_caller_may_assign(
    workspace: Workspace, owner_user: User, admin_user: User
):
    as_owner = {r["key"]: r for r in client_for(owner_user, workspace).get(f"{STAFF}roles").json()}
    as_admin = {r["key"]: r for r in client_for(admin_user, workspace).get(f"{STAFF}roles").json()}
    assert not as_owner["owner"]["assignable"] and as_owner["admin"]["assignable"]
    assert not as_admin["admin"]["assignable"] and as_admin["editor"]["assignable"]
    assert "manage_members" in as_owner["admin"]["permissions"]
    assert "manage_members" not in as_owner["editor"]["permissions"]


def test_the_list_marks_who_the_caller_may_manage(
    workspace: Workspace, owner_user: User, admin_user: User, editor_user: User
):
    rows = {r["email"]: r for r in client_for(admin_user, workspace).get(STAFF).json()}
    assert rows["admin@example.com"]["is_you"] and not rows["admin@example.com"]["can_manage"]
    assert not rows["owner@example.com"]["can_manage"]
    assert rows["editor@example.com"]["can_manage"]


# ---- creating ---------------------------------------------------------------------------------


def test_creating_staff_makes_an_account_with_a_one_time_password(
    workspace: Workspace, owner_user: User
):
    body = {"email": "New.Editor@Example.com", "full_name": "New Editor", "role": "editor"}
    res = send(client_for(owner_user, workspace), "post", STAFF, body)
    assert res.status_code == 201
    data = res.json()
    password = data["temporary_password"]
    assert password and data["invited"] is False and data["staff"]["role"] == "editor"
    user = User.objects.get(email="new.editor@example.com")
    assert user.must_change_password and user.check_password(password)
    assert Membership.objects.get(workspace=workspace, user=user).role == Role.EDITOR
    event = AuditEvent.objects.get(action="staff.created")
    assert event.actor_id == str(owner_user.pk) and password not in json.dumps(event.after)


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_an_admin_cannot_hand_out_admin_or_owner(workspace: Workspace, admin_user: User, role: str):
    res = send(
        client_for(admin_user, workspace), "post", STAFF, {"email": "x@example.com", "role": role}
    )
    assert res.status_code == 403
    assert not User.objects.filter(email="x@example.com").exists()


def test_nobody_creates_owners_through_staff(workspace: Workspace, owner_user: User):
    res = send(
        client_for(owner_user, workspace),
        "post",
        STAFF,
        {"email": "x@example.com", "role": "owner"},
    )
    assert res.status_code == 403


def test_an_existing_account_is_invited_by_email_not_taken_over(
    workspace: Workspace, owner_user: User
):
    outsider = make_user("someone@example.com")
    res = send(
        client_for(owner_user, workspace),
        "post",
        STAFF,
        {"email": outsider.email, "role": "viewer"},
    )
    assert res.status_code == 201
    assert res.json()["invited"] is True and res.json()["temporary_password"] is None
    assert not Membership.objects.filter(workspace=workspace, user=outsider).exists()
    assert mail.outbox and mail.outbox[-1].to == [outsider.email]


def test_a_current_member_cannot_be_added_twice(
    workspace: Workspace, owner_user: User, editor_user: User
):
    res = send(
        client_for(owner_user, workspace),
        "post",
        STAFF,
        {"email": editor_user.email, "role": "viewer"},
    )
    assert res.status_code == 409


# ---- changing and removing --------------------------------------------------------------------


def test_changing_role_and_record_scope(workspace: Workspace, admin_user: User, editor_user: User):
    client = client_for(admin_user, workspace)
    res = send(
        client,
        "patch",
        f"{STAFF}{editor_user.pk}",
        {"role": "analyst", "record_scope": "assigned"},
    )
    assert res.status_code == 200
    membership = Membership.objects.get(workspace=workspace, user=editor_user)
    assert membership.role == Role.ANALYST and membership.record_scope == "assigned"
    assert AuditEvent.objects.filter(action="staff.updated").exists()
    bad = send(client, "patch", f"{STAFF}{editor_user.pk}", {"record_scope": "everything"})
    assert bad.status_code in (400, 422)


def test_an_admin_cannot_promote_demote_or_remove_peers_owners_or_themselves(
    workspace: Workspace, owner_user: User, admin_user: User, editor_user: User
):
    other_admin = make_user("admin2@example.com", workspace, Role.ADMIN)
    client = client_for(admin_user, workspace)
    assert send(client, "patch", f"{STAFF}{editor_user.pk}", {"role": "admin"}).status_code == 403
    assert send(client, "patch", f"{STAFF}{other_admin.pk}", {"role": "viewer"}).status_code == 403
    assert send(client, "patch", f"{STAFF}{owner_user.pk}", {"role": "viewer"}).status_code == 403
    assert send(client, "patch", f"{STAFF}{admin_user.pk}", {"role": "viewer"}).status_code == 403
    for target in (other_admin, owner_user, admin_user):
        assert client.delete(f"{STAFF}{target.pk}").status_code == 403
    assert Membership.objects.filter(workspace=workspace).count() == 4


def test_an_owner_can_manage_admins_but_not_themselves(
    workspace: Workspace, owner_user: User, admin_user: User
):
    client = client_for(owner_user, workspace)
    assert client.delete(f"{STAFF}{owner_user.pk}").status_code == 403
    assert client.delete(f"{STAFF}{admin_user.pk}").status_code == 204
    assert not Membership.objects.filter(workspace=workspace, user=admin_user).exists()
    assert AuditEvent.objects.filter(action="staff.removed").exists()


# ---- passwords and second factors -------------------------------------------------------------


def test_resetting_a_password_gives_a_new_one_time_password_and_ends_old_access(
    workspace: Workspace, admin_user: User, editor_user: User
):
    res = send(client_for(admin_user, workspace), "post", f"{STAFF}{editor_user.pk}/reset-password")
    assert res.status_code == 200
    password = res.json()["temporary_password"]
    editor_user.refresh_from_db()
    assert editor_user.check_password(password) and not editor_user.check_password(PASSWORD)
    assert editor_user.must_change_password
    assert AuditEvent.objects.filter(action="staff.password_reset").exists()


def test_resetting_mfa_removes_the_factors(
    workspace: Workspace, admin_user: User, editor_user: User
):
    add_second_factor(editor_user)
    res = send(client_for(admin_user, workspace), "post", f"{STAFF}{editor_user.pk}/reset-mfa")
    assert res.status_code == 204
    assert not TotpDevice.objects.filter(user=editor_user).exists()
    assert AuditEvent.objects.filter(action="staff.mfa_reset").exists()


def test_accounts_shared_with_another_workspace_cannot_be_reset_here(
    workspace: Workspace, admin_user: User, editor_user: User
):
    create_workspace(name="Elsewhere", owner=make_user("boss@elsewhere.example"))
    Membership.objects.create(
        workspace=Workspace.objects.get(name="Elsewhere"), user=editor_user, role=Role.VIEWER
    )
    add_second_factor(editor_user)
    client = client_for(admin_user, workspace)
    assert send(client, "post", f"{STAFF}{editor_user.pk}/reset-password").status_code == 403
    assert send(client, "post", f"{STAFF}{editor_user.pk}/reset-mfa").status_code == 403
    editor_user.refresh_from_db()
    assert (
        editor_user.check_password(PASSWORD)
        and TotpDevice.objects.filter(user=editor_user).exists()
    )


# ---- tenant isolation -------------------------------------------------------------------------


def test_one_tenant_cannot_see_or_touch_another_tenants_staff(
    workspace: Workspace, owner_user: User
):
    other_owner = make_user("boss@other.example")
    other = create_workspace(name="Other Co", owner=other_owner)
    victim = make_user("victim@other.example", other, Role.EDITOR)
    client = client_for(owner_user, workspace)

    assert "victim@other.example" not in {r["email"] for r in client.get(STAFF).json()}
    for method, path in (
        ("patch", f"{STAFF}{victim.pk}"),
        ("delete", f"{STAFF}{victim.pk}"),
        ("post", f"{STAFF}{victim.pk}/reset-password"),
        ("post", f"{STAFF}{victim.pk}/reset-mfa"),
    ):
        assert send(client, method, path, {"role": "viewer"}).status_code == 404
    assert Membership.objects.get(workspace=other, user=victim).role == Role.EDITOR

    # Naming the other tenant in the header does not work either: the caller is not a member.
    foreign = Client(HTTP_X_WORKSPACE_ID=str(other.pk))
    foreign.force_login(owner_user)
    assert foreign.get(STAFF).status_code in (400, 404, 422)
