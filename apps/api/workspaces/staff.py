"""Staff of a tenant: who works in the workspace and what they may do.

Rules that keep this safe:
- Only roles with ``manage_members`` (owner, admin) use it, and only inside their own workspace.
- A person can hand out and manage only roles below their own (``MANAGEABLE_ROLES``); nobody
  changes, removes or resets themselves, and owners are never touched here.
- A new account gets a random temporary password, shown once, that must be changed at the first
  sign-in. An address that already has an account is invited by email instead, so nobody gains
  access to an account they do not control.
- Resetting a password or a second factor is refused for accounts that also belong to another
  workspace: that would let one tenant take over a person's access to the others.
"""

import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from identity import services as identity_services
from identity.models import User
from workspaces import invitations
from workspaces.models import Membership, RecordScope, Role
from workspaces.permissions import ROLE_PERMISSIONS, manageable_roles

NAME_LIMIT = 200


@dataclass(frozen=True, slots=True)
class StaffRow:
    membership: Membership
    mfa_enabled: bool
    is_you: bool
    can_manage: bool


@dataclass(frozen=True, slots=True)
class RoleInfo:
    key: str
    label: str
    assignable: bool
    permissions: list[str]


@dataclass(frozen=True, slots=True)
class StaffCreated:
    membership: Membership | None
    invited: bool
    temporary_password: str | None


def _can_manage(actor: Membership, target: Membership) -> bool:
    return target.user_id != actor.user_id and target.role in manageable_roles(actor.role)


def _target(actor: Membership, user_id: uuid.UUID) -> Membership:
    """A member of the actor's own workspace; anything else is not found."""
    membership = (
        Membership.objects.select_related("user")
        .filter(workspace_id=actor.workspace_id, user_id=user_id)
        .first()
    )
    if membership is None:
        raise NotFoundError("staff member not found")
    return membership


def _manageable_target(actor: Membership, user_id: uuid.UUID) -> Membership:
    target = _target(actor, user_id)
    if not _can_manage(actor, target):
        raise PermissionDeniedError("you cannot manage this person")
    return target


def _require_exclusive(target: Membership) -> None:
    shared = Membership.objects.filter(user_id=target.user_id).exclude(
        workspace_id=target.workspace_id
    )
    if shared.exists():
        raise PermissionDeniedError("this account also belongs to another workspace")


def _require_assignable(actor: Membership, role: str) -> None:
    if role not in manageable_roles(actor.role):
        raise PermissionDeniedError("you cannot give this role")


def _clean_email(value: str) -> str:
    address = value.strip().lower()
    try:
        validate_email(address)
    except DjangoValidationError as exc:
        raise ValidationFailedError("invalid email", errors={"email": "invalid"}) from exc
    return address


def list_staff(actor: Membership) -> list[StaffRow]:
    rows = Membership.objects.filter(workspace_id=actor.workspace_id).select_related("user")
    return [
        StaffRow(
            membership=m,
            mfa_enabled=identity_services.has_second_factor(m.user),
            is_you=m.user_id == actor.user_id,
            can_manage=_can_manage(actor, m),
        )
        for m in rows.order_by("user__full_name", "user__email")
    ]


def roles_catalogue(actor: Membership) -> list[RoleInfo]:
    allowed = manageable_roles(actor.role)
    return [
        RoleInfo(
            key=role.value,
            label=role.label,
            assignable=role.value in allowed,
            permissions=sorted(p.value for p in ROLE_PERMISSIONS[role]),
        )
        for role in Role
    ]


def create_staff(
    *, actor: Membership, email: str, full_name: str, role: str, record_scope: str
) -> StaffCreated:
    _require_assignable(actor, role)
    if record_scope not in RecordScope.values:
        raise ValidationFailedError("invalid record scope", errors={"record_scope": "invalid"})
    address = _clean_email(email)
    name = " ".join(full_name.split())[:NAME_LIMIT]
    if Membership.objects.filter(workspace_id=actor.workspace_id, user__email=address).exists():
        raise ConflictError("already a member")
    if User.objects.filter(email=address).exists():
        # Possession of the emailed link proves the address is theirs.
        invitations.invite(
            workspace=actor.workspace, email=address, role=role, invited_by=actor.user
        )
        return StaffCreated(membership=None, invited=True, temporary_password=None)
    with transaction.atomic():
        user = User(email=address, full_name=name, must_change_password=True)
        password = identity_services.new_temporary_password(user)
        user.set_password(password)
        user.save()
        membership = Membership.objects.create(
            workspace_id=actor.workspace_id, user=user, role=role, record_scope=record_scope
        )
        record_event(
            action="staff.created",
            actor=Actor.human(actor.user_id),
            workspace_id=actor.workspace_id,
            target_type="user",
            target_id=user.pk,
            after={"email": address, "role": role, "record_scope": record_scope},
        )
    return StaffCreated(membership=membership, invited=False, temporary_password=password)


def update_staff(
    *, actor: Membership, user_id: uuid.UUID, role: str | None, record_scope: str | None
) -> Membership:
    target = _manageable_target(actor, user_id)
    if role is not None:
        _require_assignable(actor, role)
    if record_scope is not None and record_scope not in RecordScope.values:
        raise ValidationFailedError("invalid record scope", errors={"record_scope": "invalid"})
    with transaction.atomic():
        before = {"role": target.role, "record_scope": target.record_scope}
        target.role = role or target.role
        target.record_scope = record_scope or target.record_scope
        target.save(update_fields=["role", "record_scope", "updated_at"])
        record_event(
            action="staff.updated",
            actor=Actor.human(actor.user_id),
            workspace_id=actor.workspace_id,
            target_type="user",
            target_id=user_id,
            before=before,
            after={"role": target.role, "record_scope": target.record_scope},
        )
    return target


def remove_staff(*, actor: Membership, user_id: uuid.UUID) -> None:
    target = _manageable_target(actor, user_id)
    with transaction.atomic():
        target.delete()
        record_event(
            action="staff.removed",
            actor=Actor.human(actor.user_id),
            workspace_id=actor.workspace_id,
            target_type="user",
            target_id=user_id,
            before={"role": target.role},
        )


def reset_password(*, actor: Membership, user_id: uuid.UUID) -> str:
    target = _manageable_target(actor, user_id)
    _require_exclusive(target)
    with transaction.atomic():
        password = identity_services.set_temporary_password(target.user, by=actor.user)
        record_event(
            action="staff.password_reset",
            actor=Actor.human(actor.user_id),
            workspace_id=actor.workspace_id,
            target_type="user",
            target_id=user_id,
        )
    return password


def reset_mfa(*, actor: Membership, user_id: uuid.UUID) -> None:
    target = _manageable_target(actor, user_id)
    _require_exclusive(target)
    with transaction.atomic():
        identity_services.reset_mfa(target.user, by=actor.user)
        record_event(
            action="staff.mfa_reset",
            actor=Actor.human(actor.user_id),
            workspace_id=actor.workspace_id,
            target_type="user",
            target_id=user_id,
        )
