"""Provider credentials of a workspace's SMS account. Sender numbers are ``ChannelAccount`` rows."""

from __future__ import annotations

from django.db import models

from core.models import TenantManager, TenantModel


class SmsProvider(models.TextChoices):
    TWILIO = "twilio"


class SmsCredential(TenantModel):
    """One provider account per workspace. The auth token is sealed by ``core.vault``."""

    provider = models.CharField(max_length=20, choices=SmsProvider.choices)
    account_sid = models.CharField(max_length=64)
    # Public id used in webhook URLs, so callbacks can be routed before anything is trusted.
    public_id = models.CharField(max_length=80, unique=True)
    token_ciphertext = models.BinaryField()
    token_nonce = models.BinaryField()
    token_wrapped_key = models.BinaryField()
    token_wrapped_key_nonce = models.BinaryField()
    token_key_version = models.CharField(max_length=20)
    is_active = models.BooleanField(default=True)

    objects = TenantManager["SmsCredential"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "provider"], name="uniq_sms_credential")
        ]
