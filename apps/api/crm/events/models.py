from django.db import models

from core.models import TenantManager, TenantModel


class ProcessedEvent(TenantModel):
    """Marks that a consumer handled an event, so redelivery does nothing."""

    consumer = models.CharField(max_length=100)
    event_id = models.UUIDField()

    objects = TenantManager["ProcessedEvent"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["consumer", "event_id"], name="uniq_processed_event")
        ]
