"""Staff endpoints: the people of the caller's workspace (owners and admins only)."""

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from identity.authentication import api_auth
from workspaces import staff
from workspaces.deps import resolve_workspace
from workspaces.models import Membership, RecordScope
from workspaces.permissions import Permission

router = Router(tags=["staff"], auth=api_auth)


class StaffOut(Schema):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
    record_scope: str
    mfa_enabled: bool
    must_change_password: bool
    last_login: datetime | None
    is_you: bool
    can_manage: bool


class StaffRoleOut(Schema):
    key: str
    label: str
    assignable: bool
    permissions: list[str]


class StaffCreateIn(Schema):
    email: str
    full_name: str = ""
    role: str = "viewer"
    record_scope: str = RecordScope.ALL.value


class StaffCreatedOut(Schema):
    staff: StaffOut | None
    invited: bool
    temporary_password: str | None


class StaffUpdateIn(Schema):
    role: str | None = None
    record_scope: str | None = None


class TemporaryPasswordOut(Schema):
    temporary_password: str


def _actor(request: HttpRequest) -> Membership:
    return resolve_workspace(request, Permission.MANAGE_MEMBERS)


def _out(m: Membership, *, mfa_enabled: bool, is_you: bool, can_manage: bool) -> StaffOut:
    return StaffOut(
        user_id=m.user_id,
        email=m.user.email,
        full_name=m.user.full_name,
        role=m.role,
        record_scope=m.record_scope,
        mfa_enabled=mfa_enabled,
        must_change_password=m.user.must_change_password,
        last_login=m.user.last_login,
        is_you=is_you,
        can_manage=can_manage,
    )


@router.get("/", response=list[StaffOut], operation_id="staff_list")
def list_staff(request: HttpRequest) -> list[StaffOut]:
    return [
        _out(r.membership, mfa_enabled=r.mfa_enabled, is_you=r.is_you, can_manage=r.can_manage)
        for r in staff.list_staff(_actor(request))
    ]


@router.get("/roles", response=list[StaffRoleOut], operation_id="staff_roles")
def list_roles(request: HttpRequest) -> list[StaffRoleOut]:
    return [
        StaffRoleOut(key=r.key, label=r.label, assignable=r.assignable, permissions=r.permissions)
        for r in staff.roles_catalogue(_actor(request))
    ]


@router.post("/", response={201: StaffCreatedOut}, operation_id="staff_create")
def create_staff(request: HttpRequest, payload: StaffCreateIn) -> Status[StaffCreatedOut]:
    actor = _actor(request)
    created = staff.create_staff(
        actor=actor,
        email=payload.email,
        full_name=payload.full_name,
        role=payload.role,
        record_scope=payload.record_scope,
    )
    row = (
        _out(created.membership, mfa_enabled=False, is_you=False, can_manage=True)
        if created.membership
        else None
    )
    return Status(
        201,
        StaffCreatedOut(
            staff=row, invited=created.invited, temporary_password=created.temporary_password
        ),
    )


@router.patch("/{user_id}", response=StaffOut, operation_id="staff_update")
def update_staff(request: HttpRequest, user_id: uuid.UUID, payload: StaffUpdateIn) -> StaffOut:
    actor = _actor(request)
    updated = staff.update_staff(
        actor=actor, user_id=user_id, role=payload.role, record_scope=payload.record_scope
    )
    row = next(r for r in staff.list_staff(actor) if r.membership.pk == updated.pk)
    return _out(row.membership, mfa_enabled=row.mfa_enabled, is_you=False, can_manage=True)


@router.delete("/{user_id}", response={204: None}, operation_id="staff_remove")
def remove_staff(request: HttpRequest, user_id: uuid.UUID) -> Status[None]:
    staff.remove_staff(actor=_actor(request), user_id=user_id)
    return Status(204, None)


@router.post(
    "/{user_id}/reset-password", response=TemporaryPasswordOut, operation_id="staff_reset_password"
)
def reset_password(request: HttpRequest, user_id: uuid.UUID) -> TemporaryPasswordOut:
    password = staff.reset_password(actor=_actor(request), user_id=user_id)
    return TemporaryPasswordOut(temporary_password=password)


@router.post("/{user_id}/reset-mfa", response={204: None}, operation_id="staff_reset_mfa")
def reset_mfa(request: HttpRequest, user_id: uuid.UUID) -> Status[None]:
    staff.reset_mfa(actor=_actor(request), user_id=user_id)
    return Status(204, None)
