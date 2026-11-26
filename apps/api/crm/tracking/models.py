"""Opens and clicks of tracked emails. Approximate by nature: mail apps preload images."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class EventKind(models.TextChoices):
    OPEN = "open"
    CLICK = "click"


class EmailEvent(TenantModel):
    message_id = models.UUIDField()
    contact_id = models.UUIDField()
    kind = models.CharField(max_length=10, choices=EventKind.choices)
    url = models.CharField(max_length=500, blank=True, default="")

    objects = TenantManager["EmailEvent"]()

    class Meta:
        constraints = [
            # Only the first open of a message is kept: later ones add nothing.
            models.UniqueConstraint(
                fields=["message_id"], condition=Q(kind="open"), name="uniq_first_open"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in EventKind]), name="email_event_kind_valid"
            ),
        ]
        indexes = [models.Index(fields=["workspace", "message_id", "kind"], name="email_event_idx")]
