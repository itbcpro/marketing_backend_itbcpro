"""Platform administration endpoints: tenants and their owners.

Reachable only by a platform administrator (a superuser) signed in with a session that has
completed MFA. API tokens are deliberately not accepted here. Everyone else gets "not found".
"""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from core.errors import NotFoundError
from identity.authentication import EnrolledSessionAuth
from identity.models import User
from workspaces import platform
from workspaces.models import Workspace

router = Router(tags=["platform"], auth=EnrolledSessionAuth())


class TenantOut(Schema):
    id: uuid.UUID
    name: str
    slug: str
    organization_name: str
    plan_key: str
    is_active: bool
    created_at: datetime
    member_count: int
    owner_emails: list[str]


class TenantCreateIn(Schema):
    organization_name: str
    workspace_name: str | None = None
    slug: str | None = None
    owner_email: str
    owner_full_name: str = ""


class OwnerCreatedOut(Schema):
    email: str
    existing_user: bool
    temporary_password: str | None


class TenantCreatedOut(Schema):
    tenant: TenantOut
    owner: OwnerCreatedOut


class TenantMemberOut(Schema):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
    mfa_enabled: bool


class TenantActiveIn(Schema):
    is_active: bool


class OwnerAddIn(Schema):
    email: str
    full_name: str = ""


def _admin(request: HttpRequest) -> User:
    user = request.auth  # type: ignore[attr-defined]
    if not isinstance(user, User) or not (user.is_superuser and user.is_active):
        raise NotFoundError("not found")
    return user


def _tenant(workspace: Workspace) -> TenantOut:
    owners: list[Any] = getattr(workspace, "owner_memberships", [])
    return TenantOut(
        id=workspace.pk,
        name=workspace.name,
        slug=workspace.slug,
        organization_name=workspace.organization.name,
        plan_key=workspace.organization.plan_key,
        is_active=workspace.is_active,
        created_at=workspace.created_at,
        member_count=getattr(workspace, "member_count", len(owners)),
        owner_emails=sorted(m.user.email for m in owners),
    )


def _owner(result: platform.OwnerResult) -> OwnerCreatedOut:
    return OwnerCreatedOut(
        email=result.user.email,
        existing_user=result.existing_user,
        temporary_password=result.temporary_password,
    )


@router.get("/tenants", response=list[TenantOut], operation_id="platform_tenants")
def list_tenants(request: HttpRequest) -> list[TenantOut]:
    _admin(request)
    return [_tenant(w) for w in platform.list_tenants()]


@router.post("/tenants", response={201: TenantCreatedOut}, operation_id="platform_tenant_create")
def create_tenant(request: HttpRequest, payload: TenantCreateIn) -> Status[TenantCreatedOut]:
    admin = _admin(request)
    created = platform.create_tenant(
        admin=admin,
        organization_name=payload.organization_name,
        workspace_name=payload.workspace_name,
        slug=payload.slug,
        owner_email=payload.owner_email,
        owner_full_name=payload.owner_full_name,
    )
    tenant = _tenant(platform.get_tenant(created.workspace.pk))
    return Status(201, TenantCreatedOut(tenant=tenant, owner=_owner(created.owner)))


@router.get("/tenants/{workspace_id}", response=TenantOut, operation_id="platform_tenant")
def get_tenant(request: HttpRequest, workspace_id: uuid.UUID) -> TenantOut:
    _admin(request)
    return _tenant(platform.get_tenant(workspace_id))


@router.patch(
    "/tenants/{workspace_id}", response=TenantOut, operation_id="platform_tenant_set_active"
)
def set_tenant_active(
    request: HttpRequest, workspace_id: uuid.UUID, payload: TenantActiveIn
) -> TenantOut:
    admin = _admin(request)
    platform.set_tenant_active(admin=admin, workspace_id=workspace_id, active=payload.is_active)
    return _tenant(platform.get_tenant(workspace_id))


@router.post(
    "/tenants/{workspace_id}/owners",
    response={201: OwnerCreatedOut},
    operation_id="platform_tenant_add_owner",
)
def add_owner(
    request: HttpRequest, workspace_id: uuid.UUID, payload: OwnerAddIn
) -> Status[OwnerCreatedOut]:
    admin = _admin(request)
    result = platform.add_owner(
        admin=admin, workspace_id=workspace_id, email=payload.email, full_name=payload.full_name
    )
    return Status(201, _owner(result))


@router.get(
    "/tenants/{workspace_id}/members",
    response=list[TenantMemberOut],
    operation_id="platform_tenant_members",
)
def list_members(request: HttpRequest, workspace_id: uuid.UUID) -> list[TenantMemberOut]:
    _admin(request)
    return [
        TenantMemberOut(
            user_id=m.user_id,
            email=m.user.email,
            full_name=m.user.full_name,
            role=m.role,
            mfa_enabled=has_mfa,
        )
        for m, has_mfa in platform.list_members(workspace_id)
    ]


@router.post(
    "/tenants/{workspace_id}/members/{user_id}/reset-mfa",
    response={204: None},
    operation_id="platform_member_reset_mfa",
)
def reset_member_mfa(
    request: HttpRequest, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> Status[None]:
    admin = _admin(request)
    platform.reset_member_mfa(admin=admin, workspace_id=workspace_id, user_id=user_id)
    return Status(204, None)
