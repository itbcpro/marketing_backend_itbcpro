"""Analytics tables, fed by events as things happen (never rebuilt from current state)."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class FirstResponse(TenantModel):
    """How long a customer's message waited for an answer.

    A row opens when a customer writes into a conversation that has no open row, and closes at
    the first message we send that was not blocked. Later customer messages do not reset it.
    """

    conversation_id = models.UUIDField()
    contact_id = models.UUIDField()
    channel = models.CharField(max_length=20)
    inbound_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    seconds = models.PositiveIntegerField(null=True, blank=True)
    responder_type = models.CharField(max_length=20, blank=True, default="")
    responder_id = models.CharField(max_length=100, blank=True, default="")

    objects = TenantManager["FirstResponse"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conversation_id"],
                condition=Q(responded_at__isnull=True),
                name="uniq_open_first_response",
            )
        ]
        indexes = [
            models.Index(fields=["workspace", "responded_at"], name="first_response_period_idx")
        ]
