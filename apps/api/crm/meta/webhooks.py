"""Meta webhooks: WhatsApp messages and delivery reports, Messenger messages and receipts."""

from __future__ import annotations

import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from core import tenancy
from core.errors import PermissionDeniedError, ValidationFailedError
from crm.consent.models import Channel
from crm.inbox import services as inbox
from crm.leads.tasks import capture_lead
from crm.meta import errors, templates
from crm.meta import services as meta
from crm.meta.graph import signature_of

WHATSAPP_OBJECT = "whatsapp_business_account"
PAGE_OBJECT = "page"
TEMPLATE_FIELD = "message_template_status_update"
LEADGEN_FIELD = "leadgen"
STATUS_MAP = {"sent": "sent", "delivered": "delivered", "read": "read", "failed": "failed"}
MEDIA_PLACEHOLDER = "[{kind}]"
MILLISECONDS = 1000


def verify_challenge(public_id: str, mode: str, token: str, challenge: str) -> str:
    """Answer Meta's subscription check with the challenge when the verify token matches."""
    account = meta.credential_for(public_id)
    expected = meta.secrets_of(account).verify_token
    if mode != "subscribe" or not expected or not hmac.compare_digest(expected, token):
        raise PermissionDeniedError("bad verify token")
    return challenge


def receive(public_id: str, signature: str, body: bytes) -> None:
    account = meta.credential_for(public_id)
    secret = meta.secrets_of(account).app_secret
    if not hmac.compare_digest(signature_of(secret, body), signature):
        raise PermissionDeniedError("bad signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ValidationFailedError("invalid_payload") from exc
    if not isinstance(payload, dict):
        raise ValidationFailedError("invalid_payload")
    with tenancy.use_workspace(account.workspace_id):
        for entry in payload.get("entry", []):
            _entry(account.workspace_id, str(payload.get("object", "")), entry)


def _entry(workspace_id: uuid.UUID, kind: str, entry: dict[str, Any]) -> None:
    if kind == WHATSAPP_OBJECT:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if change.get("field") == TEMPLATE_FIELD:
                _template_verdict(workspace_id, value)
            else:
                _whatsapp(workspace_id, value)
    elif kind == PAGE_OBJECT:
        for item in entry.get("messaging", []):
            _messenger(workspace_id, item)
        for change in entry.get("changes", []):
            if change.get("field") == LEADGEN_FIELD:
                _lead(workspace_id, change.get("value", {}))


def _moment(value: Any, *, milliseconds: bool = False) -> datetime:
    try:
        seconds = int(value) / (MILLISECONDS if milliseconds else 1)
        return datetime.fromtimestamp(seconds, UTC)
    except TypeError, ValueError, OverflowError, OSError:
        return datetime.now(UTC)


def _whatsapp(workspace_id: uuid.UUID, value: dict[str, Any]) -> None:
    names = {
        c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])
    }
    for item in value.get("messages", []):
        kind = str(item.get("type", "text"))
        content = item.get(kind, {}) if isinstance(item.get(kind), dict) else {}
        body = str(content.get("body") or content.get("caption") or "")
        attachments = (
            [{"kind": "media", "type": kind, "media_id": content["id"]}] if "id" in content else []
        )
        inbox.ingest(
            workspace_id=workspace_id,
            item=inbox.InboundMessage(
                channel=Channel.WHATSAPP,
                provider_message_id=str(item["id"]),
                sender_channel="phone",
                sender_value="+" + str(item["from"]),
                provider_ts=_moment(item.get("timestamp")),
                body=body or MEDIA_PLACEHOLDER.format(kind=kind),
                attachments=attachments,
                sender_name=str(names.get(item["from"], "")),
            ),
        )
    for status in value.get("statuses", []):
        mapped = STATUS_MAP.get(str(status.get("status", "")))
        if mapped is None:
            continue
        failure = (status.get("errors") or [{}])[0]
        code = failure.get("code")
        inbox.apply_status(
            workspace_id=workspace_id,
            channel=Channel.WHATSAPP,
            provider_message_id=str(status["id"]),
            status=mapped,
            provider_ts=_moment(status.get("timestamp")),
            error_code=errors.from_graph(code if isinstance(code, int) else None, 0).code
            if mapped == "failed"
            else "",
        )


def _messenger(workspace_id: uuid.UUID, item: dict[str, Any]) -> None:
    message = item.get("message")
    sender = str(item.get("sender", {}).get("id", ""))
    if isinstance(message, dict) and not message.get("is_echo") and sender:
        attachments = [
            {"kind": "media", "type": a.get("type", ""), "url": a.get("payload", {}).get("url", "")}
            for a in message.get("attachments", [])
        ]
        text = str(message.get("text", ""))
        inbox.ingest(
            workspace_id=workspace_id,
            item=inbox.InboundMessage(
                channel=Channel.MESSENGER,
                provider_message_id=str(message["mid"]),
                sender_channel="messenger",
                sender_value=sender,
                provider_ts=_moment(item.get("timestamp"), milliseconds=True),
                body=text or MEDIA_PLACEHOLDER.format(kind="attachment"),
                attachments=attachments,
            ),
        )
    delivery = item.get("delivery")
    if isinstance(delivery, dict):
        for mid in delivery.get("mids", []):
            inbox.apply_status(
                workspace_id=workspace_id,
                channel=Channel.MESSENGER,
                provider_message_id=str(mid),
                status="delivered",
                provider_ts=_moment(delivery.get("watermark"), milliseconds=True),
            )


def _template_verdict(workspace_id: uuid.UUID, value: dict[str, Any]) -> None:
    templates.apply_verdict(
        workspace_id,
        meta_id=str(value.get("message_template_id", "")),
        name=str(value.get("message_template_name", "")),
        status=str(value.get("event", "")),
        reason=str(value.get("reason") or ""),
    )


def _lead(workspace_id: uuid.UUID, value: dict[str, Any]) -> None:
    """A lead ad was submitted. The webhook carries only ids; the job fetches the answers."""
    leadgen_id = str(value.get("leadgen_id", ""))
    if not leadgen_id:
        return
    capture_lead.delay(
        workspace_id=str(workspace_id),
        leadgen_id=leadgen_id,
        form_id=str(value.get("form_id", "")),
        page_id=str(value.get("page_id", "")),
        ad_id=str(value.get("ad_id", "")),
    )
