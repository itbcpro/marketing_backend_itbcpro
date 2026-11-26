"""Provider connections and their encrypted credentials."""

from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class ConnectionStatus(models.TextChoices):
    PENDING = "pending"
    ACTIVE = "active"
    DEGRADED = "degraded"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ERROR = "error"


class CredentialKind(models.TextChoices):
    ACCESS_TOKEN = "access_token"
    REFRESH_TOKEN = "refresh_token"
    API_KEY = "api_key"
    DEVELOPER_TOKEN = "developer_token"
    CLIENT_SECRET = "client_secret"


class DocSnapshot(models.Model):
    """Last seen content hash of a vendor documentation page (drift watch)."""

    name = models.CharField(max_length=100, unique=True)
    url = models.URLField(max_length=500)
    content_hash = models.CharField(max_length=64)
    previous_hash = models.CharField(max_length=64, blank=True, default="")
    changed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class Connection(TenantModel):
    provider = models.CharField(max_length=50)
    external_account_id = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=ConnectionStatus.choices, default=ConnectionStatus.PENDING
    )
    requested_scopes = models.JSONField(default=list)
    granted_scopes = models.JSONField(default=list)
    api_version = models.CharField(max_length=20, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    last_health_at = models.DateTimeField(null=True, blank=True)
    health_detail = models.CharField(max_length=500, blank=True, default="")
    config = models.JSONField(default=dict)

    objects = TenantManager["Connection"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "provider", "external_account_id"],
                name="uniq_connection_account",
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in ConnectionStatus]),
                name="connection_status_valid",
            ),
        ]
        indexes = [models.Index(fields=["provider", "status"], name="connection_provider_idx")]


class Credential(TenantModel):
    """Encrypted secret. Never serialised to API responses or logs."""

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name="credentials")
    kind = models.CharField(max_length=30, choices=CredentialKind.choices)
    ciphertext = models.BinaryField()
    nonce = models.BinaryField()
    wrapped_key = models.BinaryField()
    wrapped_key_nonce = models.BinaryField()
    key_version = models.CharField(max_length=20)
    rotated_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Credential"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["connection", "kind"], name="uniq_credential_kind"),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in CredentialKind]),
                name="credential_kind_valid",
            ),
        ]

    def __repr__(self) -> str:
        return f"<Credential {self.kind} v={self.key_version}>"
