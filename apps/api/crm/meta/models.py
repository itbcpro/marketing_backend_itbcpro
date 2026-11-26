"""One Meta account per workspace: WhatsApp number, Facebook page and the keys that go with them."""

from __future__ import annotations

from django.db import models

from core.models import TenantManager, TenantModel


class MetaAccount(TenantModel):
    # Public id used in the webhook address, so callbacks can be routed before anything is trusted.
    public_id = models.CharField(max_length=80, unique=True)
    app_id = models.CharField(max_length=64, blank=True, default="")
    # WhatsApp Business
    waba_id = models.CharField(max_length=64, blank=True, default="")
    phone_number_id = models.CharField(max_length=64, blank=True, default="")
    display_phone = models.CharField(max_length=30, blank=True, default="")
    quality_rating = models.CharField(max_length=20, blank=True, default="")
    messaging_tier = models.CharField(max_length=40, blank=True, default="")
    status_checked_at = models.DateTimeField(null=True, blank=True)
    # Facebook page (Messenger and lead ads) and Conversions API dataset
    page_id = models.CharField(max_length=64, blank=True, default="")
    pixel_id = models.CharField(max_length=64, blank=True, default="")
    # Tokens, the app secret and the webhook verify token, sealed together by ``core.vault``.
    secrets_ciphertext = models.BinaryField()
    secrets_nonce = models.BinaryField()
    secrets_wrapped_key = models.BinaryField()
    secrets_wrapped_key_nonce = models.BinaryField()
    secrets_key_version = models.CharField(max_length=20)
    is_active = models.BooleanField(default=True)

    objects = TenantManager["MetaAccount"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace"], name="uniq_meta_account_per_workspace")
        ]


class TemplateCategory(models.TextChoices):
    MARKETING = "marketing"
    UTILITY = "utility"
    AUTHENTICATION = "authentication"


class TemplateStatus(models.TextChoices):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAUSED = "paused"
    DISABLED = "disabled"


class WhatsAppTemplate(TenantModel):
    """A message template: written here, approved by Meta, then usable outside the 24 h window."""

    name = models.CharField(max_length=100)
    language = models.CharField(max_length=10)
    category = models.CharField(max_length=20, choices=TemplateCategory.choices)
    body = models.TextField()
    # One example per {{n}} variable, in order, which Meta requires when it reviews the template.
    examples = models.JSONField(default=list)
    status = models.CharField(
        max_length=10, choices=TemplateStatus.choices, default=TemplateStatus.DRAFT
    )
    meta_template_id = models.CharField(max_length=64, blank=True, default="")
    reason = models.CharField(max_length=300, blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["WhatsAppTemplate"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name", "language"], name="uniq_wa_template"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=[s.value for s in TemplateStatus]),
                name="wa_template_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(category__in=[c.value for c in TemplateCategory]),
                name="wa_template_category_valid",
            ),
        ]
        ordering = ["name", "language"]
