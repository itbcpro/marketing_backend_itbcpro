"""Workspace tenancy and membership."""

from typing import Any

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel


class Role(models.TextChoices):
    OWNER = "owner"
    ADMIN = "admin"
    APPROVER = "approver"
    EDITOR = "editor"
    ANALYST = "analyst"
    VIEWER = "viewer"


class RecordScope(models.TextChoices):
    """Which records of a workspace a member may see (CRM records with an owner)."""

    ALL = "all"
    ASSIGNED = "assigned"


class Organization(BaseModel):
    """The paying customer or agency: owns plan and limits; its workspaces are the tenants."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80, unique=True)
    plan_key = models.CharField(max_length=50, default="free")
    limits = models.JSONField(
        default=dict, help_text="Per-plan limits that override the plan defaults."
    )
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.name


def unique_organization_slug(base: str) -> str:
    slug, n = base[:80], 1
    while Organization.objects.filter(slug=slug).exists():
        n += 1
        suffix = f"-{n}"
        slug = f"{base[: 80 - len(suffix)]}{suffix}"
    return slug


class Workspace(BaseModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="workspaces"
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80, unique=True)
    default_locale = models.CharField(max_length=10, default="en")
    reporting_currency = models.CharField(max_length=3, default="CAD")
    time_zone = models.CharField(max_length=64, default="UTC")
    is_active = models.BooleanField(default=True)
    writes_paused = models.BooleanField(
        default=False, help_text="Workspace-level kill switch for all external writes."
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(reporting_currency__regex=r"^[A-Z]{3}$"), name="workspace_currency_iso"
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.organization_id is None:
            self.organization = Organization.objects.create(
                name=self.name, slug=unique_organization_slug(self.slug)
            )
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class Invitation(BaseModel):
    """Pending invitation of an email address to a workspace. Only the token hash is stored."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField(max_length=254)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.VIEWER)
    token_hash = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="uniq_pending_invitation",
            ),
            models.CheckConstraint(
                condition=Q(role__in=[r.value for r in Role]), name="invitation_role_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.email}@{self.workspace_id}"


class Membership(BaseModel):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.VIEWER)
    record_scope = models.CharField(
        max_length=10, choices=RecordScope.choices, default=RecordScope.ALL
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(record_scope__in=[s.value for s in RecordScope]),
                name="membership_scope_valid",
            ),
            models.UniqueConstraint(fields=["workspace", "user"], name="uniq_membership"),
            models.CheckConstraint(
                condition=Q(role__in=[r.value for r in Role]), name="membership_role_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.workspace_id}:{self.role}"
