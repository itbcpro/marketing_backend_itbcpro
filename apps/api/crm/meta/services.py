"""Meta account use cases (public surface of the module)."""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone

from core import tenancy, vault
from core.errors import NotFoundError, ValidationFailedError
from crm.meta.graph import GraphClient, GraphError
from crm.meta.models import MetaAccount

ID_LIMIT = 64
PATH = "/api/v1/crm-public/meta"
SECRET_KEYS = ("whatsapp_token", "page_token", "app_secret")


@dataclass(frozen=True)
class Secrets:
    whatsapp_token: str = ""
    page_token: str = ""
    app_secret: str = ""
    verify_token: str = ""


def _aad(workspace_id: uuid.UUID, public_id: str) -> bytes:
    return f"crm.meta:{workspace_id}:{public_id}".encode()


def get_account(workspace_id: uuid.UUID) -> MetaAccount:
    found = MetaAccount.objects.for_workspace(workspace_id).filter(is_active=True).first()
    if found is None:
        raise NotFoundError("meta account not connected")
    return found


def find_account(workspace_id: uuid.UUID) -> MetaAccount | None:
    return MetaAccount.objects.for_workspace(workspace_id).filter(is_active=True).first()


def secrets_of(account: MetaAccount) -> Secrets:
    sealed = vault.SealedSecret(
        ciphertext=bytes(account.secrets_ciphertext),
        nonce=bytes(account.secrets_nonce),
        wrapped_key=bytes(account.secrets_wrapped_key),
        wrapped_key_nonce=bytes(account.secrets_wrapped_key_nonce),
        key_version=account.secrets_key_version,
    )
    raw = vault.open_secret(sealed, aad=_aad(account.workspace_id, account.public_id))
    return Secrets(**json.loads(raw))


def _seal(account: MetaAccount, value: Secrets) -> None:
    payload = json.dumps(value.__dict__).encode()
    sealed = vault.seal(payload, aad=_aad(account.workspace_id, account.public_id))
    account.secrets_ciphertext = sealed.ciphertext
    account.secrets_nonce = sealed.nonce
    account.secrets_wrapped_key = sealed.wrapped_key
    account.secrets_wrapped_key_nonce = sealed.wrapped_key_nonce
    account.secrets_key_version = sealed.key_version


def connect(
    workspace_id: uuid.UUID,
    *,
    app_secret: str,
    app_id: str = "",
    whatsapp_token: str = "",
    page_token: str = "",
    waba_id: str = "",
    phone_number_id: str = "",
    page_id: str = "",
    pixel_id: str = "",
) -> MetaAccount:
    """Store (or replace) the workspace's Meta account. Blank tokens keep what is stored."""
    ids = (app_id, waba_id, phone_number_id, page_id, pixel_id)
    if not app_secret or any(len(i) > ID_LIMIT for i in ids):
        raise ValidationFailedError("invalid_credentials")
    if (whatsapp_token or phone_number_id) and not (whatsapp_token and phone_number_id):
        raise ValidationFailedError("whatsapp_needs_token_and_number")
    account = MetaAccount.objects.for_workspace(workspace_id).first()
    previous = Secrets()
    if account is None:
        account = MetaAccount(
            workspace_id=workspace_id, public_id=f"{workspace_id.hex}.{secrets.token_urlsafe(24)}"
        )
        previous = Secrets(verify_token=secrets.token_urlsafe(24))
    else:
        previous = secrets_of(account)
    _seal(
        account,
        Secrets(
            whatsapp_token=whatsapp_token or previous.whatsapp_token,
            page_token=page_token or previous.page_token,
            app_secret=app_secret,
            verify_token=previous.verify_token,
        ),
    )
    account.app_id = app_id
    account.waba_id, account.phone_number_id = waba_id, phone_number_id
    account.page_id, account.pixel_id = page_id, pixel_id
    account.is_active = True
    account.save()
    return account


def disconnect(workspace_id: uuid.UUID) -> None:
    get_account(workspace_id).delete()


def credential_for(public_id: str) -> MetaAccount:
    """Find the account a webhook is addressed to. Any problem is plain not found."""
    prefix, _, _ = public_id.partition(".")
    try:
        workspace_id = uuid.UUID(hex=prefix)
    except ValueError as exc:
        raise NotFoundError("not found") from exc
    with tenancy.use_workspace(workspace_id):
        found = (
            MetaAccount.objects.for_workspace(workspace_id)
            .filter(public_id=public_id, is_active=True)
            .first()
        )
    if found is None:
        raise NotFoundError("not found")
    return found


# Tests and the simulator replace this to route requests to a fake Meta.
transport_factory: Callable[[], httpx.BaseTransport] | None = None


def client(token: str) -> GraphClient:
    return GraphClient(token, transport=transport_factory() if transport_factory else None)


def webhook_url(account: MetaAccount) -> str:
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{PATH}/{account.public_id}"


def refresh_status(workspace_id: uuid.UUID) -> MetaAccount:
    """Ask Meta for the number's quality rating and messaging tier."""
    account = get_account(workspace_id)
    if not account.phone_number_id:
        raise ValidationFailedError("no_whatsapp_number")
    token = secrets_of(account).whatsapp_token
    try:
        data = client(token).get(
            f"/{account.phone_number_id}",
            {"fields": "display_phone_number,quality_rating,messaging_limit_tier"},
        )
    except GraphError as exc:
        raise ValidationFailedError(exc.code) from exc
    account.display_phone = str(data.get("display_phone_number", ""))
    account.quality_rating = str(data.get("quality_rating", ""))
    account.messaging_tier = str(data.get("messaging_limit_tier", ""))
    account.status_checked_at = timezone.now()
    account.save(
        update_fields=[
            "display_phone",
            "quality_rating",
            "messaging_tier",
            "status_checked_at",
            "updated_at",
        ]
    )
    return account


def finish_embedded_signup(
    workspace_id: uuid.UUID, *, code: str, waba_id: str, phone_number_id: str
) -> MetaAccount:
    """Complete WhatsApp embedded signup: trade the code for a token and subscribe to webhooks."""
    account = get_account(workspace_id)
    current = secrets_of(account)
    if not account.app_id or not code or not waba_id or not phone_number_id:
        raise ValidationFailedError("invalid_signup")
    try:
        exchanged = client("").get(
            "/oauth/access_token",
            {"client_id": account.app_id, "client_secret": current.app_secret, "code": code},
        )
        token = str(exchanged.get("access_token", ""))
        if not token:
            raise ValidationFailedError("invalid_signup")
        client(token).post(f"/{waba_id}/subscribed_apps", {})
    except GraphError as exc:
        raise ValidationFailedError(exc.code) from exc
    _seal(account, Secrets(**{**current.__dict__, "whatsapp_token": token}))
    account.waba_id, account.phone_number_id = waba_id, phone_number_id
    account.save()
    return account


LEAD_FIELDS = "field_data,created_time,ad_id,adset_id,campaign_id,form_id"


def fetch_lead(workspace_id: uuid.UUID, leadgen_id: str) -> dict[str, Any]:
    """Read one lead ad submission from Meta (the webhook only carries its id)."""
    account = get_account(workspace_id)
    token = secrets_of(account).page_token
    if not token:
        raise ValidationFailedError("no_page_token")
    return client(token).get(f"/{leadgen_id}", {"fields": LEAD_FIELDS})


def send_conversions(workspace_id: uuid.UUID, events: list[dict[str, Any]]) -> None:
    """Report conversions to the account's Conversions API dataset."""
    account = get_account(workspace_id)
    token = secrets_of(account).page_token
    if not account.pixel_id or not token:
        raise ValidationFailedError("no_conversions_dataset")
    client(token).post(f"/{account.pixel_id}/events", {"data": events})


__all__ = [
    "GraphError",
    "Secrets",
    "connect",
    "disconnect",
    "finish_embedded_signup",
    "get_account",
    "secrets_of",
]
