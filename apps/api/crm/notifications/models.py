"""Notifications for the people of a workspace, and what each person wants to receive."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class NotificationKind(models.TextChoices):
    TASK_ASSIGNED = "task_assigned"
    MENTION = "mention"
    CONTACT_ASSIGNED = "contact_assigned"
    DEAL_ASSIGNED = "deal_assigned"
    CONVERSATION_ASSIGNED = "conversation_assigned"
    CUSTOMER_REPLIED = "customer_replied"


class Notification(TenantModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=30, choices=NotificationKind.choices)
    # Ids of the things concerned (contact, task, deal, conversation); the client fetches details.
    data = models.JSONField(default=dict)
    read_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Notification"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in NotificationKind]),
                name="crm_notification_kind_valid",
            )
        ]
        indexes = [
            models.Index(
                fields=["workspace", "user", "read_at", "-created_at"], name="notif_inbox_idx"
            ),
            models.Index(
                fields=["workspace", "user", "created_at"],
                condition=Q(emailed_at__isnull=True, read_at__isnull=True),
                name="notif_digest_idx",
            ),
        ]
        ordering = ["-created_at", "-id"]


class Preference(TenantModel):
    """One row per person and kind, created when they change a default."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=30, choices=NotificationKind.choices)
    in_app = models.BooleanField(default=True)
    email_digest = models.BooleanField(default=False)

    objects = TenantManager["Preference"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user", "kind"], name="uniq_notification_preference"
            )
        ]
