"""Webhook endpoints, signature checks and processing (public surface of the module)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core import tenancy, vault
from core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from crm.consent.models import Channel
from crm.sending import services as sending
from crm.webhooks.models import DeliveryStatus, WebhookDelivery, WebhookEndpoint, WebhookKind

SIGNATURE_HEADER = "X-Signature"
SECRET_BYTES = 32
NAME_LIMIT = 100


@dataclass(frozen=True)
class Created:
    endpoint: WebhookEndpoint
    secret: str  # shown once; only the sealed form is stored


def _aad(workspace_id: uuid.UUID, public_id: str) -> bytes:
    return f"crm.webhook:{workspace_id}:{public_id}".encode()


def _seal(endpoint: WebhookEndpoint, secret: str) -> None:
    sealed = vault.seal(secret.encode(), aad=_aad(endpoint.workspace_id, endpoint.public_id))
    endpoint.secret_ciphertext = sealed.ciphertext
    endpoint.secret_nonce = sealed.nonce
    endpoint.secret_wrapped_key = sealed.wrapped_key
    endpoint.secret_wrapped_key_nonce = sealed.wrapped_key_nonce
    endpoint.secret_key_version = sealed.key_version


def _secret_of(endpoint: WebhookEndpoint) -> bytes:
    sealed = vault.SealedSecret(
        ciphertext=bytes(endpoint.secret_ciphertext),
        nonce=bytes(endpoint.secret_nonce),
        wrapped_key=bytes(endpoint.secret_wrapped_key),
        wrapped_key_nonce=bytes(endpoint.secret_wrapped_key_nonce),
        key_version=endpoint.secret_key_version,
    )
    return vault.open_secret(sealed, aad=_aad(endpoint.workspace_id, endpoint.public_id))


# ---- management ----------------------------------------------------------------------


def list_endpoints(workspace_id: uuid.UUID) -> list[WebhookEndpoint]:
    return list(WebhookEndpoint.objects.for_workspace(workspace_id).order_by("created_at"))


def get_endpoint(workspace_id: uuid.UUID, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    found = WebhookEndpoint.objects.for_workspace(workspace_id).filter(pk=endpoint_id).first()
    if found is None:
        raise NotFoundError("webhook endpoint not found")
    return found


def create_endpoint(workspace_id: uuid.UUID, *, kind: str, name: str) -> Created:
    if kind not in WebhookKind.values:
        raise ValidationFailedError("invalid_kind")
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > NAME_LIMIT:
        raise ValidationFailedError("invalid_name")
    secret = secrets.token_urlsafe(SECRET_BYTES)
    endpoint = WebhookEndpoint(
        workspace_id=workspace_id,
        kind=kind,
        name=cleaned,
        public_id=f"{workspace_id.hex}.{secrets.token_urlsafe(24)}",
    )
    _seal(endpoint, secret)
    endpoint.save()
    return Created(endpoint, secret)


def rotate_secret(workspace_id: uuid.UUID, endpoint_id: uuid.UUID) -> Created:
    endpoint = get_endpoint(workspace_id, endpoint_id)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    _seal(endpoint, secret)
    endpoint.save()
    return Created(endpoint, secret)


def set_active(workspace_id: uuid.UUID, endpoint_id: uuid.UUID, active: bool) -> WebhookEndpoint:
    endpoint = get_endpoint(workspace_id, endpoint_id)
    endpoint.is_active = active
    endpoint.save(update_fields=["is_active", "updated_at"])
    return endpoint


def delete_endpoint(workspace_id: uuid.UUID, endpoint_id: uuid.UUID) -> None:
    get_endpoint(workspace_id, endpoint_id).delete()


def sign(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    """The header value a sender (or a test) puts on a request."""
    stamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


# ---- receiving -----------------------------------------------------------------------


def _find(public_id: str) -> WebhookEndpoint:
    """Look the endpoint up inside the workspace its id names. Any problem is plain not found."""
    prefix, _, _ = public_id.partition(".")
    try:
        workspace_id = uuid.UUID(hex=prefix)
    except ValueError as exc:
        raise NotFoundError("not found") from exc
    with tenancy.use_workspace(workspace_id):
        found = (
            WebhookEndpoint.objects.for_workspace(workspace_id)
            .filter(public_id=public_id, is_active=True)
            .first()
        )
    if found is None:
        raise NotFoundError("not found")
    return found


def _verify(endpoint: WebhookEndpoint, header: str, body: bytes) -> None:
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        stamp = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError) as exc:
        raise PermissionDeniedError("bad signature") from exc
    if abs(time.time() - stamp) > settings.CRM_WEBHOOK_TOLERANCE_SECONDS:
        raise PermissionDeniedError("bad signature")
    expected = hmac.new(_secret_of(endpoint), f"{stamp}.".encode() + body, hashlib.sha256)
    if not hmac.compare_digest(expected.hexdigest(), given):
        raise PermissionDeniedError("bad signature")


def _email_events(workspace_id: uuid.UUID, body: bytes) -> None:
    try:
        data = json.loads(body)
        kind = str(data["type"])
        provider_id = str(data["provider_message_id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationFailedError("invalid_event") from exc
    if kind == "bounce":
        sending.record_bounce(
            workspace_id=workspace_id,
            channel=Channel.EMAIL,
            provider_message_id=provider_id,
            hard=bool(data.get("hard", True)),
        )
    elif kind == "complaint":
        sending.record_complaint(
            workspace_id=workspace_id, channel=Channel.EMAIL, provider_message_id=provider_id
        )
    else:
        raise ValidationFailedError("invalid_event")


def _inbound_email(workspace_id: uuid.UUID, body: bytes) -> None:
    sending.ingest_email(workspace_id=workspace_id, raw=body)


HANDLERS: dict[str, Callable[[uuid.UUID, bytes], None]] = {
    WebhookKind.INBOUND_EMAIL: _inbound_email,
    WebhookKind.EMAIL_EVENTS: _email_events,
}


def receive(public_id: str, signature: str, body: bytes) -> WebhookDelivery:
    """Verify, archive once, then process. A repeated body returns the first delivery."""
    if len(body) > settings.CRM_WEBHOOK_MAX_BODY_BYTES:
        raise ValidationFailedError("body_too_large")
    endpoint = _find(public_id)
    workspace_id = endpoint.workspace_id
    with tenancy.use_workspace(workspace_id):
        _verify(endpoint, signature, body)
        digest = hashlib.sha256(body).hexdigest()
        try:
            with transaction.atomic():
                delivery = WebhookDelivery.objects.create(
                    workspace_id=workspace_id, endpoint=endpoint, body_sha256=digest, body=body
                )
        except IntegrityError:
            return WebhookDelivery.objects.for_workspace(workspace_id).get(
                endpoint=endpoint, body_sha256=digest
            )
        WebhookEndpoint.objects.for_workspace(workspace_id).filter(pk=endpoint.pk).update(
            last_received_at=timezone.now()
        )
        try:
            with transaction.atomic():
                HANDLERS[endpoint.kind](workspace_id, body)
            delivery.status = DeliveryStatus.PROCESSED
        except ValidationFailedError as exc:
            delivery.status = DeliveryStatus.FAILED
            delivery.error = str(exc)[:300]
        delivery.save(update_fields=["status", "error", "updated_at"])
    return delivery


def recent_deliveries(
    workspace_id: uuid.UUID, endpoint_id: uuid.UUID, limit: int = 50
) -> list[WebhookDelivery]:
    endpoint = get_endpoint(workspace_id, endpoint_id)
    rows = WebhookDelivery.objects.for_workspace(workspace_id).filter(endpoint=endpoint)
    return list(rows.order_by("-created_at")[:limit])
