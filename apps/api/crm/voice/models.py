"""The call log: every call in or out of the workspace, with its recording and transcript."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class CallDirection(models.TextChoices):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(models.TextChoices):
    RINGING = "ringing"
    COMPLETED = "completed"
    MISSED = "missed"  # nobody picked up; the caller may have left a voicemail
    FAILED = "failed"


class Call(TenantModel):
    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="calls"
    )
    direction = models.CharField(max_length=10, choices=CallDirection.choices)
    provider_call_id = models.CharField(max_length=64)
    from_number = models.CharField(max_length=20)
    to_number = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=CallStatus.choices, default=CallStatus.RINGING)
    duration_seconds = models.PositiveIntegerField(default=0)
    voicemail = models.BooleanField(default=False)
    recording_url = models.CharField(max_length=500, blank=True, default="")
    recording_seconds = models.PositiveIntegerField(default=0)
    transcript = models.TextField(blank=True, default="")
    text_back_sent = models.BooleanField(default=False)

    objects = TenantManager["Call"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "provider_call_id"], name="uniq_call_provider_id"
            ),
            models.CheckConstraint(
                condition=Q(direction__in=[d.value for d in CallDirection]),
                name="call_direction_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in CallStatus]), name="call_status_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "-created_at"], name="call_recent_idx"),
            models.Index(fields=["workspace", "contact", "-created_at"], name="call_contact_idx"),
        ]
        ordering = ["-created_at", "-id"]
