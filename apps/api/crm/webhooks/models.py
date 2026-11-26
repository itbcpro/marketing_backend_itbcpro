"""Public webhook endpoints (one signing secret each) and the raw archive of what arrived."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class WebhookKind(models.TextChoices):
    INBOUND_EMAIL = "inbound_email"  # a raw RFC 822 message as the request body
    EMAIL_EVENTS = "email_events"  # JSON bounce and complaint notices


class WebhookEndpoint(TenantModel):
    kind = models.CharField(max_length=30, choices=WebhookKind.choices)
    name = models.CharField(max_length=100)
    public_id = models.CharField(max_length=80, unique=True)
    # The signing secret, sealed by core.vault. Never stored or logged in clear.
    secret_ciphertext = models.BinaryField()
    secret_nonce = models.BinaryField()
    secret_wrapped_key = models.BinaryField()
    secret_wrapped_key_nonce = models.BinaryField()
    secret_key_version = models.CharField(max_length=20)
    is_active = models.BooleanField(default=True)
    last_received_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["WebhookEndpoint"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in WebhookKind]), name="webhook_kind_valid"
            )
        ]


class DeliveryStatus(models.TextChoices):
    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"


class WebhookDelivery(TenantModel):
    """One verified request, kept as received. The body hash makes a repeat harmless."""

    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries"
    )
    body_sha256 = models.CharField(max_length=64)
    body = models.BinaryField()
    status = models.CharField(
        max_length=10, choices=DeliveryStatus.choices, default=DeliveryStatus.RECEIVED
    )
    error = models.CharField(max_length=300, blank=True, default="")

    objects = TenantManager["WebhookDelivery"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["endpoint", "body_sha256"], name="uniq_webhook_body")
        ]
        indexes = [models.Index(fields=["workspace", "created_at"], name="webhook_delivery_idx")]
