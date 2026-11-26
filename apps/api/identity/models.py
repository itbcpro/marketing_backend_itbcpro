"""Custom user model and authentication-related tables.

Created before the first migration so the user model never has to be swapped.
"""

from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import BaseModel


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def get_by_natural_key(self, username: str | None) -> User:
        return self.get(email=(username or "").strip().lower())

    def _create(self, email: str, password: str | None, **extra: Any) -> User:
        if not email:
            raise ValueError("An email address is required")
        user = self.model(email=email.strip().lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self._create(email, password, **extra)


class User(BaseModel, AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(max_length=254, unique=True)
    full_name = models.CharField(max_length=200, blank=True, default="")
    locale = models.CharField(max_length=10, default="en")
    time_zone = models.CharField(max_length=64, default="UTC")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(
        default=False, help_text="Set when an administrator chose the password."
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="uniq_user_email_lower"),
        ]

    def __str__(self) -> str:
        return str(self.email)

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > timezone.now())


class TotpDevice(BaseModel):
    """Time-based one-time password enrolment. The secret is stored encrypted."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="totp_device")
    ciphertext = models.BinaryField()
    nonce = models.BinaryField()
    wrapped_key = models.BinaryField()
    wrapped_key_nonce = models.BinaryField()
    key_version = models.CharField(max_length=20)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_step = models.BigIntegerField(default=0)

    def __str__(self) -> str:
        return f"totp:{self.user_id}"


class RecoveryCode(BaseModel):
    """Single-use backup code; only a hash is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "code_hash"], name="uniq_recovery_code"),
        ]

    def __str__(self) -> str:
        return f"recovery:{self.user_id}"


class PasskeyCredential(BaseModel):
    """WebAuthn credential registered by a user."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="passkeys")
    name = models.CharField(max_length=100)
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.BigIntegerField(default=0)
    transports = models.JSONField(default=list)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.user_id})"


class ApiToken(BaseModel):
    """Long-lived machine credential. Only the SHA-256 of the secret part is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=12, unique=True)
    token_hash = models.CharField(max_length=64)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix})"


class SessionRecord(BaseModel):
    """Metadata about a browser session so users can review and revoke devices."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="session_records")
    session_key = models.CharField(max_length=40, unique=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    last_seen_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"session:{self.user_id}"


class RefreshToken(BaseModel):
    """One single-use refresh token. Tokens of one login share a family so reuse can revoke all."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="+"
    )
    family_id = models.UUIDField(db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    idle_expires_at = models.DateTimeField()
    rotated_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "revoked_at"], name="refresh_user_active_idx")]

    def __str__(self) -> str:
        return f"refresh:{self.family_id}"
