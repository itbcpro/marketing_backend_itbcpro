"""Addresses we must not send to, whatever the consent says."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class SuppressionReason(models.TextChoices):
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"
    MANUAL = "manual"


class Suppression(TenantModel):
    """A suppressed address on one channel. Hard bounces and spam complaints land here."""

    channel = models.CharField(max_length=20)
    address = models.CharField(max_length=254)
    reason = models.CharField(max_length=20, choices=SuppressionReason.choices)
    note = models.CharField(max_length=300, blank=True, default="")

    objects = TenantManager["Suppression"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "channel", "address"], name="uniq_suppression"
            ),
            models.CheckConstraint(
                condition=Q(reason__in=[r.value for r in SuppressionReason]),
                name="suppression_reason_valid",
            ),
        ]


class DomainStatus(models.TextChoices):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class SendingDomain(TenantModel):
    """A domain (usually a subdomain such as mail.brand.com) mail may be sent from."""

    domain = models.CharField(max_length=253, unique=True)
    status = models.CharField(
        max_length=10, choices=DomainStatus.choices, default=DomainStatus.PENDING
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["SendingDomain"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in DomainStatus]),
                name="sending_domain_status_valid",
            )
        ]


class RecordStatus(models.TextChoices):
    PENDING = "pending"
    OK = "ok"
    MISSING = "missing"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class DomainRecord(TenantModel):
    """One DNS record the owner must publish, and what was last seen in DNS."""

    domain = models.ForeignKey(SendingDomain, on_delete=models.CASCADE, related_name="records")
    purpose = models.CharField(max_length=20)
    record_type = models.CharField(max_length=5)
    name = models.CharField(max_length=253)
    value = models.CharField(max_length=1000)
    priority = models.PositiveSmallIntegerField(null=True, blank=True)
    required = models.BooleanField(default=True)
    status = models.CharField(
        max_length=10, choices=RecordStatus.choices, default=RecordStatus.PENDING
    )
    observed = models.CharField(max_length=2000, blank=True, default="")
    checked_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["DomainRecord"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["domain", "purpose", "name"], name="uniq_domain_record"
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in RecordStatus]),
                name="domain_record_status_valid",
            ),
        ]
        ordering = ["purpose", "name"]
