"""WhatsApp (Cloud API) and Messenger through the workspace's own Meta account."""

from __future__ import annotations

from typing import Any

from crm.consent.models import Channel
from crm.meta import errors
from crm.meta import services as meta
from crm.sending.ports import (
    Capabilities,
    OutboundRequest,
    PermanentAdapterError,
    RetryableAdapterError,
    SendResult,
)

WHATSAPP_MAX_BODY = 4096
MESSENGER_MAX_BODY = 2000


def _kind(request: OutboundRequest, kind: str) -> dict[str, Any] | None:
    return next((a for a in request.attachments if a.get("kind") == kind), None)


def _post(token: str, path: str, body: dict[str, Any]) -> str:
    try:
        data = meta.client(token).post(path, body)
    except meta.GraphError as exc:
        if exc.retryable:
            raise RetryableAdapterError(exc.code) from exc
        raise PermanentAdapterError(exc.code) from exc
    messages = data.get("messages")
    if isinstance(messages, list) and messages:
        return str(messages[0]["id"])
    return str(data.get("message_id", ""))


class MetaWhatsAppAdapter:
    channel: str = Channel.WHATSAPP
    capabilities: Capabilities = Capabilities(
        max_body_length=WHATSAPP_MAX_BODY, supports_attachments=True
    )

    def send(self, request: OutboundRequest) -> SendResult:
        account = meta.find_account(request.workspace_id)
        if account is None:
            raise PermanentAdapterError(errors.NO_ACCOUNT)
        token = meta.secrets_of(account).whatsapp_token
        if not account.phone_number_id or not token:
            raise PermanentAdapterError(errors.NO_SENDER)
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": request.to.lstrip("+"),
            # Comes back on status callbacks, so a message can be traced to our own record.
            "biz_opaque_callback_data": str(request.message_id),
        }
        template = _kind(request, "template")
        if template:
            payload["type"] = "template"
            payload["template"] = {
                "name": template["name"],
                "language": {"code": template["language"]},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(v)} for v in template.get("variables", [])
                        ],
                    }
                ],
            }
        else:
            image = next(
                (
                    a
                    for a in request.attachments
                    if str(a.get("content_type", "")).startswith("image/")
                ),
                None,
            )
            if image and image.get("url"):
                payload["type"] = "image"
                payload["image"] = {"link": image["url"], "caption": request.body}
            else:
                payload["type"] = "text"
                payload["text"] = {"body": request.body}
        return SendResult(
            provider_message_id=_post(token, f"/{account.phone_number_id}/messages", payload)
        )


class MetaMessengerAdapter:
    channel: str = Channel.MESSENGER
    capabilities: Capabilities = Capabilities(max_body_length=MESSENGER_MAX_BODY)

    def send(self, request: OutboundRequest) -> SendResult:
        account = meta.find_account(request.workspace_id)
        if account is None:
            raise PermanentAdapterError(errors.NO_ACCOUNT)
        token = meta.secrets_of(account).page_token
        if not account.page_id or not token:
            raise PermanentAdapterError(errors.NO_SENDER)
        payload: dict[str, Any] = {
            "recipient": {"id": request.to},
            "message": {"text": request.body},
            "messaging_type": "RESPONSE",
        }
        tag = _kind(request, "tag")
        if tag:
            payload["messaging_type"] = "MESSAGE_TAG"
            payload["tag"] = tag["tag"]
        return SendResult(provider_message_id=_post(token, f"/{account.page_id}/messages", payload))
