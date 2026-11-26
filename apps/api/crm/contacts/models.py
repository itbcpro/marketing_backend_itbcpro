"""Contacts, their identities, companies, tags and merge records."""

from __future__ import annotations

import uuid
from typing import Self

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from core.models import HasPk, TenantManager, TenantModel, TenantQuerySet


class ContactType(models.TextChoices):
    LEAD = "lead"
    CUSTOMER = "customer"


class MatchOrder(models.TextChoices):
    """Which identity decides first when inbound data could match a contact."""

    EMAIL_FIRST = "email_first"
    PHONE_FIRST = "phone_first"


class IdentityChannel(models.TextChoices):
    EMAIL = "email"
    PHONE = "phone"
    WHATSAPP = "whatsapp"
    MESSENGER = "messenger"
    INSTAGRAM = "instagram"
    WEB_CHAT = "web_chat"


class CrmSettings(TenantModel):
    """Per-workspace CRM behaviour. One row per workspace, created on first use."""

    match_order = models.CharField(
        max_length=20, choices=MatchOrder.choices, default=MatchOrder.EMAIL_FIRST
    )
    round_robin_cursor = models.UUIDField(null=True, blank=True)

    objects = TenantManager["CrmSettings"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace"], name="uniq_crm_settings_workspace"),
            models.CheckConstraint(
                condition=Q(match_order__in=[m.value for m in MatchOrder]),
                name="crm_settings_match_order_valid",
            ),
        ]


class Company(TenantModel):
    name = models.CharField(max_length=200)
    email = models.CharField(max_length=254, blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")
    website = models.CharField(max_length=300, blank=True, default="")
    address = models.CharField(max_length=300, blank=True, default="")
    description = models.TextField(blank=True, default="")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Company"]()

    class Meta:
        indexes = [models.Index(fields=["workspace", "name"], name="company_name_idx")]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ContactQuerySet(TenantQuerySet["Contact"]):
    def active(self) -> Self:
        return self.filter(deleted_at__isnull=True)


class ContactManager(TenantManager["Contact"]):
    def get_queryset(self) -> ContactQuerySet:
        return ContactQuerySet(self.model, using=self._db)

    def for_workspace(self, workspace: HasPk | uuid.UUID) -> ContactQuerySet:
        return self.get_queryset().for_workspace(workspace)


class Contact(TenantModel):
    first_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100, blank=True, default="")
    email = models.CharField(max_length=254, blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")
    company = models.ForeignKey(
        Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="contacts"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    contact_type = models.CharField(
        max_length=10, choices=ContactType.choices, default=ContactType.LEAD
    )
    source = models.CharField(max_length=100, blank=True, default="")
    time_zone = models.CharField(max_length=64, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    address = models.CharField(max_length=300, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    postal_code = models.CharField(max_length=20, blank=True, default="")
    country = models.CharField(max_length=2, blank=True, default="")
    attribution = models.JSONField(default=dict)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    merged_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="merged_from"
    )

    objects = ContactManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=Q(deleted_at__isnull=True) & ~Q(email=""),
                name="uniq_contact_email",
            ),
            models.UniqueConstraint(
                fields=["workspace", "phone"],
                condition=Q(deleted_at__isnull=True) & ~Q(phone=""),
                name="uniq_contact_phone",
            ),
            models.CheckConstraint(
                condition=Q(contact_type__in=[t.value for t in ContactType]),
                name="contact_type_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["workspace", "assigned_to", "-created_at"], name="contact_owner_idx"
            ),
            models.Index(fields=["workspace", "last_name", "first_name"], name="contact_name_idx"),
        ]

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self) -> str:
        return self.full_name or self.email or self.phone or str(self.pk)


class ContactIdentity(TenantModel):
    """Every address a contact can be recognised by. Unique per workspace, channel and value."""

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="identities")
    channel = models.CharField(max_length=20, choices=IdentityChannel.choices)
    value = models.CharField(max_length=254)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    objects = TenantManager["ContactIdentity"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "channel", "value"],
                condition=Q(is_active=True),
                name="uniq_contact_identity",
            ),
            models.CheckConstraint(
                condition=Q(channel__in=[c.value for c in IdentityChannel]),
                name="contact_identity_channel_valid",
            ),
        ]
        indexes = [models.Index(fields=["contact"], name="contact_identity_contact_idx")]


class ContactFollower(TenantModel):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="followers")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")

    objects = TenantManager["ContactFollower"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["contact", "user"], name="uniq_contact_follower")
        ]


class Tag(TenantModel):
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=7, blank=True, default="")

    objects = TenantManager["Tag"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("name"), "workspace", name="uniq_tag_name_ci"),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ContactTag(TenantModel):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="tag_links")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="contact_links")

    objects = TenantManager["ContactTag"]()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["contact", "tag"], name="uniq_contact_tag")]
        indexes = [models.Index(fields=["tag"], name="contact_tag_tag_idx")]


class ContactMerge(TenantModel):
    """Record of one merge, with what is needed to undo it inside the undo window."""

    winner = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="+")
    loser = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="+")
    snapshot = models.JSONField(default=dict)
    merged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    undone_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["ContactMerge"]()
