"""Workspace use cases."""

import uuid

from django.db import transaction
from django.utils.text import slugify

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, PermissionDeniedError
from identity.models import User
from workspaces.models import Membership, Organization, RecordScope, Role, Workspace
from workspaces.permissions import Permission, role_allows


def create_workspace(
    *,
    name: str,
    owner: User,
    slug: str | None = None,
    organization: Organization | None = None,
    **fields: str,
) -> Workspace:
    slug = slug or slugify(name)
    if Workspace.objects.filter(slug=slug).exists():
        raise ConflictError("workspace slug already exists")
    with transaction.atomic():
        workspace = Workspace.objects.create(
            name=name, slug=slug, organization=organization, **fields
        )
        Membership.objects.create(workspace=workspace, user=owner, role=Role.OWNER)
        record_event(
            action="workspace.created",
            actor=Actor.human(owner.pk),
            workspace_id=workspace.pk,
            target_type="workspace",
            target_id=workspace.pk,
            after={"name": name, "slug": slug},
        )
    return workspace


def set_record_scope(
    *, workspace: Workspace, user: User, scope: RecordScope, changed_by: User
) -> Membership:
    """Limit (or widen) which owned records a member can see."""
    require_permission(changed_by, workspace.pk, Permission.MANAGE_MEMBERS)
    with transaction.atomic():
        membership = (
            Membership.objects.select_for_update().filter(workspace=workspace, user=user).first()
        )
        if membership is None:
            raise NotFoundError("membership not found")
        before = membership.record_scope
        membership.record_scope = scope
        membership.save(update_fields=["record_scope", "updated_at"])
        record_event(
            action="membership.record_scope_changed",
            actor=Actor.human(changed_by.pk),
            workspace_id=workspace.pk,
            target_type="membership",
            target_id=membership.pk,
            before={"record_scope": before},
            after={"record_scope": scope},
        )
    return membership


def add_member(*, workspace: Workspace, user: User, role: Role, added_by: User) -> Membership:
    require_permission(added_by, workspace.pk, Permission.MANAGE_MEMBERS)
    with transaction.atomic():
        membership, created = Membership.objects.update_or_create(
            workspace=workspace, user=user, defaults={"role": role}
        )
        record_event(
            action="membership.created" if created else "membership.role_changed",
            actor=Actor.human(added_by.pk),
            workspace_id=workspace.pk,
            target_type="membership",
            target_id=membership.pk,
            after={"user_id": str(user.pk), "role": str(role)},
        )
    return membership


def get_membership(user: User, workspace_id: uuid.UUID) -> Membership:
    try:
        return Membership.objects.select_related("workspace").get(
            user=user, workspace_id=workspace_id, workspace__is_active=True
        )
    except Membership.DoesNotExist as exc:
        raise NotFoundError("workspace not found") from exc


def require_permission(user: User, workspace_id: uuid.UUID, permission: Permission) -> Membership:
    membership = get_membership(user, workspace_id)
    if not role_allows(membership.role, permission):
        raise PermissionDeniedError(f"missing permission {permission}")
    return membership


def list_members(workspace_id: uuid.UUID) -> list[Membership]:
    """Everyone in the workspace, with their user, in a stable order for pickers."""
    rows = Membership.objects.filter(workspace_id=workspace_id).select_related("user")
    return list(rows.order_by("user__full_name", "user__email"))
