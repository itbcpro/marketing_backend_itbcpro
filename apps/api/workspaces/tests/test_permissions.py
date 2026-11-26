import pytest

from conftest import make_user
from core.errors import ConflictError, NotFoundError, PermissionDeniedError
from identity.models import User
from workspaces import services
from workspaces.models import Membership, Role, Workspace
from workspaces.permissions import ROLE_PERMISSIONS, Permission, role_allows

pytestmark = pytest.mark.django_db

EXPECTED = {
    Role.OWNER: set(Permission),
    Role.ADMIN: set(Permission) - {Permission.MANAGE_WORKSPACE},
    Role.APPROVER: {
        Permission.VIEW,
        Permission.EDIT_CONTENT,
        Permission.APPROVE_R1,
        Permission.APPROVE_R2,
        Permission.USE_KILL_SWITCH,
        Permission.VIEW_AUDIT,
        Permission.CRM_VIEW,
        Permission.CRM_EDIT,
        Permission.CRM_SEND,
        Permission.CRM_VIEW_REPORTS,
    },
    Role.EDITOR: {
        Permission.VIEW,
        Permission.EDIT_CONTENT,
        Permission.EDIT_MEMORY,
        Permission.CRM_VIEW,
        Permission.CRM_EDIT,
        Permission.CRM_SEND,
    },
    Role.ANALYST: {
        Permission.VIEW,
        Permission.VIEW_AUDIT,
        Permission.CRM_VIEW,
        Permission.CRM_VIEW_REPORTS,
    },
    Role.VIEWER: {Permission.VIEW, Permission.CRM_VIEW},
}


@pytest.mark.django_db(False)
@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("permission", list(Permission))
def test_permission_matrix(role: Role, permission: Permission) -> None:
    assert role_allows(role, permission) is (permission in EXPECTED[role])


@pytest.mark.django_db(False)
def test_every_role_is_covered_and_r3_approval_is_owner_and_admin_only() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)
    holders = {r for r in Role if role_allows(r, Permission.APPROVE_R3)}
    assert holders == {Role.OWNER, Role.ADMIN}


def test_create_workspace_makes_owner_and_audits(owner: User) -> None:
    workspace = services.create_workspace(name="Second Brand", owner=owner)
    assert Membership.objects.get(workspace=workspace, user=owner).role == Role.OWNER
    with pytest.raises(ConflictError):
        services.create_workspace(name="Second Brand", owner=owner)


def test_require_permission_enforces_membership_and_role(workspace: Workspace) -> None:
    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    stranger = make_user("stranger@example.com")
    services.require_permission(viewer, workspace.pk, Permission.VIEW)
    with pytest.raises(PermissionDeniedError):
        services.require_permission(viewer, workspace.pk, Permission.APPROVE_R1)
    with pytest.raises(NotFoundError):
        services.require_permission(stranger, workspace.pk, Permission.VIEW)


def test_only_members_managers_can_add_members(workspace: Workspace, owner: User) -> None:
    editor = make_user("editor@example.com", workspace, Role.EDITOR)
    newcomer = make_user("new@example.com")
    with pytest.raises(PermissionDeniedError):
        services.add_member(workspace=workspace, user=newcomer, role=Role.VIEWER, added_by=editor)
    membership = services.add_member(
        workspace=workspace, user=newcomer, role=Role.ANALYST, added_by=owner
    )
    assert membership.role == Role.ANALYST


def test_inactive_workspace_is_not_reachable(workspace: Workspace, owner: User) -> None:
    Workspace.objects.filter(pk=workspace.pk).update(is_active=False)
    with pytest.raises(NotFoundError):
        services.get_membership(owner, workspace.pk)


# ---- record scope ---------------------------------------------------------------------------


def test_assigned_scope_limits_visible_records(owner: User, workspace: Workspace) -> None:
    from django.db.models import QuerySet

    from workspaces.models import RecordScope
    from workspaces.scope import can_see, restrict

    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    services.set_record_scope(
        workspace=workspace, user=rep, scope=RecordScope.ASSIGNED, changed_by=owner
    )
    membership = Membership.objects.get(workspace=workspace, user=rep)
    assert can_see(membership, rep.pk) and not can_see(membership, owner.pk)
    qs = Membership.objects.filter(workspace=workspace)
    assert isinstance(restrict(qs, membership, owner_field="user_id"), QuerySet)
    assert list(restrict(qs, membership, owner_field="user_id")) == [membership]
    owner_membership = Membership.objects.get(workspace=workspace, user=owner)
    assert restrict(qs, owner_membership, owner_field="user_id").count() == qs.count()


def test_only_member_managers_can_change_scope(owner: User, workspace: Workspace) -> None:
    from workspaces.models import RecordScope

    rep = make_user("rep2@example.com", workspace, Role.EDITOR)
    with pytest.raises(PermissionDeniedError):
        services.set_record_scope(
            workspace=workspace, user=rep, scope=RecordScope.ASSIGNED, changed_by=rep
        )
    with pytest.raises(NotFoundError):
        services.set_record_scope(
            workspace=workspace,
            user=make_user("nobody@example.com"),
            scope=RecordScope.ASSIGNED,
            changed_by=owner,
        )
