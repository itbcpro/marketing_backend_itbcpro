"""Lead ad forms, the leads they brought in, and the rules that report results back to Meta."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class LeadFormMapping(TenantModel):
    """How the questions of one lead form land on a contact."""

    form_id = models.CharField(max_length=64)
    name = models.CharField(max_length=100, blank=True, default="")
    # {question name: contact field}; ``custom.<key>`` targets a custom field. Standard questions
    # (full name, email, phone, city and so on) need no entry.
    mapping = models.JSONField(default=dict)
    tags = models.JSONField(default=list)
    # Channels the form's own consent text covers, e.g. ["email", "sms"]. Nothing is assumed.
    consent_channels = models.JSONField(default=list)

    objects = TenantManager["LeadFormMapping"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "form_id"], name="uniq_lead_form")
        ]


class LeadCapture(TenantModel):
    """One lead, kept once however many times Meta delivers the webhook."""

    leadgen_id = models.CharField(max_length=64)
    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="lead_captures"
    )
    form_id = models.CharField(max_length=64, blank=True, default="")
    page_id = models.CharField(max_length=64, blank=True, default="")
    ad_id = models.CharField(max_length=64, blank=True, default="")
    adset_id = models.CharField(max_length=64, blank=True, default="")
    campaign_id = models.CharField(max_length=64, blank=True, default="")
    answers = models.JSONField(default=dict)

    objects = TenantManager["LeadCapture"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "leadgen_id"], name="uniq_lead_capture")
        ]
        indexes = [models.Index(fields=["workspace", "-created_at"], name="lead_recent_idx")]
        ordering = ["-created_at", "-id"]


class ConversionTrigger(models.TextChoices):
    LEAD_CAPTURED = "lead_captured"
    DEAL_CREATED = "deal_created"
    DEAL_WON = "deal_won"
    STAGE_REACHED = "stage_reached"


class ConversionRule(TenantModel):
    """When something happens in the CRM, tell Meta about it under this event name."""

    name = models.CharField(max_length=100)
    trigger = models.CharField(max_length=20, choices=ConversionTrigger.choices)
    stage_id = models.UUIDField(null=True, blank=True)  # for ``stage_reached``
    event_name = models.CharField(max_length=60)
    send_value = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    objects = TenantManager["ConversionRule"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(trigger__in=[t.value for t in ConversionTrigger]),
                name="conversion_trigger_valid",
            )
        ]


class ConversionOutcome(models.TextChoices):
    SENT = "sent"
    SKIPPED_NO_CONSENT = "skipped_no_consent"
    SKIPPED_NO_DATA = "skipped_no_data"
    FAILED = "failed"


class ConversionLog(TenantModel):
    """What was reported to Meta and why something was not. One row per rule and source event."""

    rule = models.ForeignKey(ConversionRule, on_delete=models.CASCADE, related_name="logs")
    event_id = models.CharField(max_length=100)
    contact_id = models.UUIDField()
    outcome = models.CharField(max_length=20, choices=ConversionOutcome.choices)
    detail = models.CharField(max_length=200, blank=True, default="")

    objects = TenantManager["ConversionLog"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rule", "event_id"], name="uniq_conversion_log")
        ]
        ordering = ["-created_at", "-id"]
