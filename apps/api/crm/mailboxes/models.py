"""Connected mailboxes: where a person's (or the team's) email is sent from and read back."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel, TenantManager, TenantModel


class Provider(models.TextChoices):
    SMTP = "smtp"
    GOOGLE = "google"
    MICROSOFT = "microsoft"


class MailboxStatus(models.TextChoices):
    CONNECTED = "connected"
    NEEDS_ATTENTION = "needs_attention"  # the provider refused our credentials
    DISABLED = "disabled"  # disconnected or switched off; nothing is sent from it


class Mailbox(TenantModel):
    """One email account. ``owner`` empty means a shared mailbox of the workspace."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    provider = models.CharField(max_length=10, choices=Provider.choices)
    email = models.CharField(max_length=254)
    display_name = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=MailboxStatus.choices, default=MailboxStatus.CONNECTED
    )
    last_error = models.CharField(max_length=100, blank=True, default="")
    daily_limit = models.PositiveIntegerField()
    # Non-secret settings: SMTP host, port and security; the scopes an OAuth grant covers.
    config = models.JSONField(default=dict)
    sync_enabled = models.BooleanField(default=False)
    sync_cursor = models.CharField(max_length=100, blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    # Passwords and OAuth tokens, sealed by core.vault. Never stored or logged in clear.
    secret_ciphertext = models.BinaryField(default=b"")
    secret_nonce = models.BinaryField(default=b"")
    secret_wrapped_key = models.BinaryField(default=b"")
    secret_wrapped_key_nonce = models.BinaryField(default=b"")
    secret_key_version = models.CharField(max_length=20, blank=True, default="")
    # The sender in the inbox that messages sent from this mailbox are recorded against.
    account = models.ForeignKey(
        "crm_inbox.ChannelAccount",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = TenantManager["Mailbox"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "email"], name="uniq_mailbox_email"),
            models.CheckConstraint(
                condition=Q(provider__in=[p.value for p in Provider]), name="mailbox_provider_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in MailboxStatus]),
                name="mailbox_status_valid",
            ),
        ]
        indexes = [models.Index(fields=["workspace", "owner"], name="mailbox_owner_idx")]
        ordering = ["email"]


class AuthRequest(TenantModel):
    """A started OAuth sign-in. The ``state`` value ties the browser's return to this request."""

    state_hash = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    provider = models.CharField(max_length=10, choices=Provider.choices)
    verifier_ciphertext = models.BinaryField()
    verifier_nonce = models.BinaryField()
    verifier_wrapped_key = models.BinaryField()
    verifier_wrapped_key_nonce = models.BinaryField()
    verifier_key_version = models.CharField(max_length=20)
    sync = models.BooleanField(default=False)
    shared = models.BooleanField(default=False)
    expires_at = models.DateTimeField()

    objects = TenantManager["AuthRequest"]()


class PlatformOAuthSettings(BaseModel):
    """The OAuth apps the platform administrator registered. One row, edited in the platform panel.

    Not tenant data: every workspace signs in through the same Google and Microsoft apps.
    Secrets are sealed by the vault and never returned by the API.
    """

    google_client_id = models.CharField(max_length=200, blank=True, default="")
    microsoft_client_id = models.CharField(max_length=200, blank=True, default="")
    microsoft_tenant = models.CharField(max_length=128, blank=True, default="")
    redirect_uri = models.CharField(max_length=500, blank=True, default="")
    return_url = models.CharField(max_length=500, blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    google_client_secret_ciphertext = models.BinaryField(default=b"")
    google_client_secret_nonce = models.BinaryField(default=b"")
    google_client_secret_wrapped_key = models.BinaryField(default=b"")
    google_client_secret_wrapped_key_nonce = models.BinaryField(default=b"")
    google_client_secret_key_version = models.CharField(max_length=20, blank=True, default="")
    microsoft_client_secret_ciphertext = models.BinaryField(default=b"")
    microsoft_client_secret_nonce = models.BinaryField(default=b"")
    microsoft_client_secret_wrapped_key = models.BinaryField(default=b"")
    microsoft_client_secret_wrapped_key_nonce = models.BinaryField(default=b"")
    microsoft_client_secret_key_version = models.CharField(max_length=20, blank=True, default="")
