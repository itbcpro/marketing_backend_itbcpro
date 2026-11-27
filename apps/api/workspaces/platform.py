"""Platform administration: tenants (an organization with its workspace) and their owners.

Only platform administrators (superusers) reach these use cases; the API layer enforces that.
A new owner gets a random temporary password that must be changed at the first sign-in. It is
returned once and never stored in clear text or written to the audit log.
"""

import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Prefetch, QuerySet
from django.utils.text import slugify

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from identity import services as identity_services
from identity.models import User
from workspaces.models import Membership, Organization, Role, Workspace, unique_organization_slug

NAME_LIMIT = 200
SLUG_LIMIT = 80


@dataclass(frozen=True, slots=True)
class OwnerResult:
    user: User
    existing_user: bool
    temporary_password: str | None


@dataclass(frozen=True, slots=True)
class TenantCreated:
    workspace: Workspace
    owner: OwnerResult


def _clean_name(value: str, field: str) -> str:
    name = " ".join(value.split())
    if not name or len(name) > NAME_LIMIT:
        raise ValidationFailedError("invalid name", errors={field: "invalid"})
    return name


def _clean_email(value: str) -> str:
    address = value.strip().lower()
    try:
        validate_email(address)
    except DjangoValidationError as exc:
        raise ValidationFailedError("invalid email", errors={"owner_email": "invalid"}) from exc
    return address


def _workspace_slug(requested: str | None, name: str) -> str:
    base = slugify(requested or name)[:SLUG_LIMIT]
    if not base:
        raise ValidationFailedError("invalid slug", errors={"slug": "invalid"})
    if Workspace.objects.filter(slug=base).exists():
        if requested:
            raise ConflictError("workspace slug already exists")
        base = f"{base[: SLUG_LIMIT - 7]}-{uuid.uuid4().hex[:6]}"
    return base


def _ensure_owner(*, workspace: Workspace, email: str, full_name: str) -> OwnerResult:
    """Make the address an owner of the workspace, creating the account when it is new."""
    user = User.objects.filter(email=email).first()
    password: str | None = None
    if user is None:
        user = User(email=email, full_name=full_name[:NAME_LIMIT], must_change_password=True)
        password = identity_services.new_temporary_password(user)
        user.set_password(password)
        user.save()
    Membership.objects.update_or_create(
        workspace=workspace, user=user, defaults={"role": Role.OWNER}
    )
    return OwnerResult(user=user, existing_user=password is None, temporary_password=password)


def create_tenant(
    *,
    admin: User,
    organization_name: str,
    workspace_name: str | None,
    slug: str | None,
    owner_email: str,
    owner_full_name: str,
) -> TenantCreated:
    org_name = _clean_name(organization_name, "organization_name")
    ws_name = _clean_name(workspace_name, "workspace_name") if workspace_name else org_name
    address = _clean_email(owner_email)
    with transaction.atomic():
        organization = Organization.objects.create(
            name=org_name, slug=unique_organization_slug(slugify(org_name) or "organization")
        )
        workspace = Workspace.objects.create(
            organization=organization, name=ws_name, slug=_workspace_slug(slug, ws_name)
        )
        owner = _ensure_owner(workspace=workspace, email=address, full_name=owner_full_name.strip())
        record_event(
            action="platform.tenant_created",
            actor=Actor.human(admin.pk),
            workspace_id=workspace.pk,
            target_type="workspace",
            target_id=workspace.pk,
            after={"name": ws_name, "organization": org_name, "owner": address},
        )
    return TenantCreated(workspace=workspace, owner=owner)


def add_owner(*, admin: User, workspace_id: uuid.UUID, email: str, full_name: str) -> OwnerResult:
    workspace = get_tenant(workspace_id)
    address = _clean_email(email)
    with transaction.atomic():
        owner = _ensure_owner(workspace=workspace, email=address, full_name=full_name.strip())
        record_event(
            action="platform.owner_added",
            actor=Actor.human(admin.pk),
            workspace_id=workspace.pk,
            target_type="workspace",
            target_id=workspace.pk,
            after={"owner": address, "new_account": not owner.existing_user},
        )
    return owner


def set_tenant_active(*, admin: User, workspace_id: uuid.UUID, active: bool) -> Workspace:
    with transaction.atomic():
        workspace = Workspace.objects.select_for_update().filter(pk=workspace_id).first()
        if workspace is None:
            raise NotFoundError("tenant not found")
        if workspace.is_active != active:
            workspace.is_active = active
            workspace.save(update_fields=["is_active", "updated_at"])
            record_event(
                action="platform.tenant_activated" if active else "platform.tenant_deactivated",
                actor=Actor.human(admin.pk),
                workspace_id=workspace.pk,
                target_type="workspace",
                target_id=workspace.pk,
                before={"is_active": not active},
                after={"is_active": active},
            )
    return workspace


def _tenants() -> QuerySet[Workspace]:
    owners = Membership.objects.filter(role=Role.OWNER).select_related("user")
    return (
        Workspace.objects.select_related("organization")
        .annotate(member_count=Count("memberships"))
        .prefetch_related(Prefetch("memberships", queryset=owners, to_attr="owner_memberships"))
    )


def list_tenants() -> list[Workspace]:
    return list(_tenants().order_by("-created_at"))


def get_tenant(workspace_id: uuid.UUID) -> Workspace:
    workspace = _tenants().filter(pk=workspace_id).first()
    if workspace is None:
        raise NotFoundError("tenant not found")
    return workspace


def list_members(workspace_id: uuid.UUID) -> list[tuple[Membership, bool]]:
    """Members of a tenant with whether each has a second factor set up."""
    get_tenant(workspace_id)
    rows = Membership.objects.filter(workspace_id=workspace_id).select_related("user")
    ordered = rows.order_by("role", "user__email")
    return [(m, identity_services.has_second_factor(m.user)) for m in ordered]


def reset_member_mfa(*, admin: User, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Take away a member's second factors. Platform administrators cannot be reset here."""
    membership = (
        Membership.objects.select_related("user")
        .filter(workspace_id=workspace_id, user_id=user_id, user__is_superuser=False)
        .first()
    )
    if membership is None:
        raise NotFoundError("member not found")
    with transaction.atomic():
        identity_services.reset_mfa(membership.user, by=admin)
        record_event(
            action="platform.member_mfa_reset",
            actor=Actor.human(admin.pk),
            workspace_id=workspace_id,
            target_type="user",
            target_id=user_id,
        )
