"""SMS account use cases (public surface of the module)."""

from __future__ import annotations

import hmac
import secrets
import uuid
from collections.abc import Callable

import httpx
from django.conf import settings

from core import tenancy, vault
from core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from crm.sms.models import SmsCredential, SmsProvider
from crm.sms.twilio_client import SmsSendError, TwilioClient, expected_signature

CALLBACK_PATH = "/api/v1/crm-public/sms/twilio"
SID_LIMIT = 64


def _aad(workspace_id: uuid.UUID, public_id: str) -> bytes:
    return f"crm.sms:{workspace_id}:{public_id}".encode()


def get_credential(workspace_id: uuid.UUID, provider: str = SmsProvider.TWILIO) -> SmsCredential:
    found = (
        SmsCredential.objects.for_workspace(workspace_id)
        .filter(provider=provider, is_active=True)
        .first()
    )
    if found is None:
        raise NotFoundError("sms account not connected")
    return found


def connect(
    workspace_id: uuid.UUID,
    *,
    account_sid: str,
    auth_token: str,
    provider: str = SmsProvider.TWILIO,
) -> SmsCredential:
    """Store (or replace) the provider account. The token is sealed and never read back out."""
    sid = account_sid.strip()
    if provider not in SmsProvider.values or not sid or len(sid) > SID_LIMIT or not auth_token:
        raise ValidationFailedError("invalid_credentials")
    row = SmsCredential.objects.for_workspace(workspace_id).filter(provider=provider).first()
    if row is None:
        row = SmsCredential(
            workspace_id=workspace_id,
            provider=provider,
            public_id=f"{workspace_id.hex}.{secrets.token_urlsafe(24)}",
        )
    sealed = vault.seal(auth_token.encode(), aad=_aad(workspace_id, row.public_id))
    row.account_sid = sid
    row.token_ciphertext = sealed.ciphertext
    row.token_nonce = sealed.nonce
    row.token_wrapped_key = sealed.wrapped_key
    row.token_wrapped_key_nonce = sealed.wrapped_key_nonce
    row.token_key_version = sealed.key_version
    row.is_active = True
    row.save()
    return row


def disconnect(workspace_id: uuid.UUID, provider: str = SmsProvider.TWILIO) -> None:
    get_credential(workspace_id, provider).delete()


def auth_token_of(credential: SmsCredential) -> str:
    sealed = vault.SealedSecret(
        ciphertext=bytes(credential.token_ciphertext),
        nonce=bytes(credential.token_nonce),
        wrapped_key=bytes(credential.token_wrapped_key),
        wrapped_key_nonce=bytes(credential.token_wrapped_key_nonce),
        key_version=credential.token_key_version,
    )
    return vault.open_secret(
        sealed, aad=_aad(credential.workspace_id, credential.public_id)
    ).decode()


# Tests and the simulator replace this to route requests to a fake provider.
transport_factory: Callable[[], httpx.BaseTransport] | None = None


def client_for(credential: SmsCredential) -> TwilioClient:
    transport = transport_factory() if transport_factory else None
    return TwilioClient(credential.account_sid, auth_token_of(credential), transport=transport)


def credential_for(public_id: str) -> SmsCredential:
    """Find the account a callback is addressed to. Any problem is plain not found."""
    prefix, _, _ = public_id.partition(".")
    try:
        workspace_id = uuid.UUID(hex=prefix)
    except ValueError as exc:
        raise NotFoundError("not found") from exc
    with tenancy.use_workspace(workspace_id):
        found = (
            SmsCredential.objects.for_workspace(workspace_id)
            .filter(public_id=public_id, is_active=True)
            .first()
        )
    if found is None:
        raise NotFoundError("not found")
    return found


def verify_signature(
    credential: SmsCredential, url: str, signature: str, params: dict[str, str]
) -> None:
    expected = expected_signature(auth_token_of(credential), url, params)
    if not hmac.compare_digest(expected, signature):
        raise PermissionDeniedError("bad signature")


def callback_url(credential: SmsCredential, kind: str) -> str:
    """Where the provider reports delivery (``status``) and replies (``inbound``)."""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}{CALLBACK_PATH}/{credential.public_id}/{kind}"


__all__ = [
    "SmsSendError",
    "auth_token_of",
    "callback_url",
    "client_for",
    "connect",
    "credential_for",
    "disconnect",
    "get_credential",
    "verify_signature",
]
