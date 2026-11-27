"""The OAuth apps (Google, Microsoft) the platform administrator registered, kept in the database.

One row for the whole platform, edited from the platform panel. Secrets are sealed by the vault and
never returned. A value left empty in the database falls back to the environment, so a deployment
that already uses environment variables keeps working until the panel is used.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction

from audit.actors import Actor
from audit.services import record_event
from core import vault
from core.errors import ValidationFailedError
from crm.mailboxes.models import PlatformOAuthSettings

ID_LIMIT = 200
TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
SECRET_FIELDS = ("google_client_secret", "microsoft_client_secret")
TEXT_FIELDS = ("google_client_id", "microsoft_client_id", "microsoft_tenant")
URL_FIELDS = ("redirect_uri", "return_url")


@dataclass(frozen=True)
class OAuthConfig:
    google_client_id: str
    google_client_secret: str
    microsoft_client_id: str
    microsoft_client_secret: str
    microsoft_tenant: str
    redirect_uri: str
    return_url: str


def _aad(row: PlatformOAuthSettings, field: str) -> bytes:
    return f"crm.mailbox.platform:{row.pk}:{field}".encode()


def _open(row: PlatformOAuthSettings, field: str) -> str:
    cipher = bytes(getattr(row, f"{field}_ciphertext"))
    if not cipher:
        return ""
    sealed = vault.SealedSecret(
        ciphertext=cipher,
        nonce=bytes(getattr(row, f"{field}_nonce")),
        wrapped_key=bytes(getattr(row, f"{field}_wrapped_key")),
        wrapped_key_nonce=bytes(getattr(row, f"{field}_wrapped_key_nonce")),
        key_version=getattr(row, f"{field}_key_version"),
    )
    return vault.open_secret(sealed, aad=_aad(row, field)).decode()


def _seal(row: PlatformOAuthSettings, field: str, value: str) -> None:
    if not value:
        for part in ("ciphertext", "nonce", "wrapped_key", "wrapped_key_nonce"):
            setattr(row, f"{field}_{part}", b"")
        setattr(row, f"{field}_key_version", "")
        return
    sealed = vault.seal(value.encode(), aad=_aad(row, field))
    setattr(row, f"{field}_ciphertext", sealed.ciphertext)
    setattr(row, f"{field}_nonce", sealed.nonce)
    setattr(row, f"{field}_wrapped_key", sealed.wrapped_key)
    setattr(row, f"{field}_wrapped_key_nonce", sealed.wrapped_key_nonce)
    setattr(row, f"{field}_key_version", sealed.key_version)


def _row() -> PlatformOAuthSettings | None:
    return PlatformOAuthSettings.objects.order_by("created_at").first()


def current() -> OAuthConfig:
    """What is in force: the database value where set, otherwise the environment's."""
    row = _row()

    def pick(field: str, fallback: str) -> str:
        if row is None:
            return fallback
        value = _open(row, field) if field in SECRET_FIELDS else str(getattr(row, field))
        return value or fallback

    return OAuthConfig(
        google_client_id=pick("google_client_id", settings.CRM_GOOGLE_CLIENT_ID),
        google_client_secret=pick("google_client_secret", settings.CRM_GOOGLE_CLIENT_SECRET),
        microsoft_client_id=pick("microsoft_client_id", settings.CRM_MICROSOFT_CLIENT_ID),
        microsoft_client_secret=pick(
            "microsoft_client_secret", settings.CRM_MICROSOFT_CLIENT_SECRET
        ),
        microsoft_tenant=pick("microsoft_tenant", settings.CRM_MICROSOFT_TENANT) or "common",
        redirect_uri=pick("redirect_uri", settings.CRM_MAILBOX_REDIRECT_URI),
        return_url=pick("return_url", settings.CRM_MAILBOX_RETURN_URL),
    )


def source_of(provider: str) -> str:
    """Where the provider's keys come from: ``database``, ``environment`` or ``none``."""
    row = _row()
    id_field = f"{provider}_client_id"
    secret_field = f"{provider}_client_secret"
    if (
        row is not None
        and getattr(row, id_field)
        and bool(bytes(getattr(row, f"{secret_field}_ciphertext")))
    ):
        return "database"
    env = (
        (settings.CRM_GOOGLE_CLIENT_ID, settings.CRM_GOOGLE_CLIENT_SECRET)
        if provider == "google"
        else (settings.CRM_MICROSOFT_CLIENT_ID, settings.CRM_MICROSOFT_CLIENT_SECRET)
    )
    return "environment" if all(env) else "none"


def secret_is_set(provider: str) -> bool:
    row = _row()
    stored = row is not None and bool(bytes(getattr(row, f"{provider}_client_secret_ciphertext")))
    env = (
        settings.CRM_GOOGLE_CLIENT_SECRET
        if provider == "google"
        else settings.CRM_MICROSOFT_CLIENT_SECRET
    )
    return stored or bool(env)


def stored_values() -> dict[str, str]:
    """The non-secret values saved in the database (empty strings where unset)."""
    row = _row()
    return {f: (str(getattr(row, f)) if row else "") for f in (*TEXT_FIELDS, *URL_FIELDS)}


def _clean_url(field: str, value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    parsed = urlparse(text)
    secure = parsed.scheme == "https" or (
        parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS
    )
    if not secure or not parsed.netloc or parsed.fragment or parsed.username:
        raise ValidationFailedError("invalid url", errors={field: "invalid_url"})
    return text


def _clean_changes(changes: dict[str, Any]) -> dict[str, str]:
    """Validate the non-secret fields given; raises with every problem at once."""
    errors: dict[str, str] = {}
    cleaned: dict[str, str] = {}
    for field in TEXT_FIELDS:
        if changes.get(field) is None:
            continue
        text = str(changes[field]).strip()
        tenant_bad = field == "microsoft_tenant" and text and not TENANT_PATTERN.match(text)
        if len(text) > ID_LIMIT or tenant_bad:
            errors[field] = "invalid_value"
        cleaned[field] = text
    for field in URL_FIELDS:
        if changes.get(field) is None:
            continue
        try:
            cleaned[field] = _clean_url(field, str(changes[field]))
        except ValidationFailedError:
            errors[field] = "invalid_url"
    if errors:
        raise ValidationFailedError("invalid settings", errors=errors)
    return cleaned


def save(*, admin_id: uuid.UUID, changes: dict[str, Any]) -> None:
    """Apply the fields given. A text field set to "" clears it; a secret of None is kept."""
    cleaned = _clean_changes(changes)
    with transaction.atomic():
        row = PlatformOAuthSettings.objects.select_for_update().order_by("created_at").first()
        row = row or PlatformOAuthSettings()
        for field, value in cleaned.items():
            setattr(row, field, value)
        touched = list(cleaned)
        for field in SECRET_FIELDS:
            if changes.get(field) is not None:
                _seal(row, field, str(changes[field]).strip())
                touched.append(field)
        row.updated_by_id = admin_id
        row.save()
        # The names of what changed are logged, never a secret.
        record_event(
            action="platform.mailbox_oauth_updated",
            actor=Actor.human(admin_id),
            target_type="platform_oauth_settings",
            target_id=row.pk,
            after={"fields": sorted(touched)},
        )
