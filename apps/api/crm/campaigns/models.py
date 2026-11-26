"""Campaigns: one message sent to an audience in batches, always through the send gateway."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class CampaignStatus(models.TextChoices):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    SENDING = "sending"
    PAUSED = "paused"
    DONE = "done"
    CANCELED = "canceled"


class Campaign(TenantModel):
    name = models.CharField(max_length=120)
    channel = models.CharField(max_length=20)
    purpose = models.CharField(max_length=20, default="marketing")
    subject = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField()
    audience = models.JSONField(default=dict, help_text="Contact filter; empty means everyone.")
    track_opens = models.BooleanField(default=False)
    track_clicks = models.BooleanField(default=False)
    # An A/B test: this share of the audience gets variant B (blank parts fall back to A).
    variant_subject = models.CharField(max_length=300, blank=True, default="")
    variant_body = models.TextField(blank=True, default="")
    variant_percent = models.PositiveSmallIntegerField(default=0)
    channel_account = models.ForeignKey(
        "crm_inbox.ChannelAccount",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    status = models.CharField(
        max_length=10, choices=CampaignStatus.choices, default=CampaignStatus.DRAFT
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    batch_size = models.PositiveIntegerField()
    batch_interval_seconds = models.PositiveIntegerField()
    next_batch_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["Campaign"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in CampaignStatus]),
                name="campaign_status_valid",
            ),
            models.CheckConstraint(condition=Q(batch_size__gte=1), name="campaign_batch_positive"),
            models.CheckConstraint(
                condition=Q(variant_percent__lte=50), name="campaign_variant_percent_ok"
            ),
        ]
        indexes = [models.Index(fields=["status", "next_batch_at"], name="campaign_due_idx")]
        ordering = ["-created_at"]


class RecipientStatus(models.TextChoices):
    PENDING = "pending"
    SENT = "sent"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class CampaignRecipient(TenantModel):
    """One contact of a campaign. The audience is frozen when the campaign starts."""

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    contact = models.ForeignKey("crm_contacts.Contact", on_delete=models.CASCADE, related_name="+")
    variant = models.CharField(max_length=1, default="A")
    status = models.CharField(
        max_length=10, choices=RecipientStatus.choices, default=RecipientStatus.PENDING
    )
    reason = models.CharField(max_length=60, blank=True, default="")
    message = models.ForeignKey(
        "crm_inbox.Message", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    processed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["CampaignRecipient"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["campaign", "contact"], name="uniq_campaign_recipient"),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in RecipientStatus]),
                name="recipient_status_valid",
            ),
        ]
        indexes = [models.Index(fields=["campaign", "status"], name="recipient_status_idx")]
