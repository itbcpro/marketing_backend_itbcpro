"""Instagram direct messages through the connection the social module already holds.

No token is copied: the adapter asks the social module's client factory for a client and sends
with it, so a refreshed or revoked token is picked up from one place.
"""

from __future__ import annotations

from core.errors import AuthExpiredError, ProviderError, RetryableProviderError
from crm.consent.models import Channel
from crm.inbox import services as inbox
from crm.sending.ports import (
    Capabilities,
    OutboundRequest,
    PermanentAdapterError,
    RetryableAdapterError,
    SendResult,
)
from social.instagram import factory
from social.models import SocialAccount

MAX_BODY = 1000
NO_ACCOUNT = "instagram_not_connected"
AUTH_EXPIRED = "instagram_token_expired"
REJECTED = "instagram_rejected"
UNAVAILABLE = "instagram_unavailable"


def _account(request: OutboundRequest) -> SocialAccount | None:
    rows = SocialAccount.objects.filter(workspace_id=request.workspace_id, network="instagram")
    sender = request.from_address
    if not sender:
        default = inbox.default_sender(request.workspace_id, Channel.INSTAGRAM)
        sender = default.external_id if default else ""
    if sender:
        rows = rows.filter(external_id=sender)
    return rows.select_related("connection").order_by("created_at").first()


class InstagramAdapter:
    channel: str = Channel.INSTAGRAM
    capabilities: Capabilities = Capabilities(max_body_length=MAX_BODY)

    def send(self, request: OutboundRequest) -> SendResult:
        account = _account(request)
        if account is None:
            raise PermanentAdapterError(NO_ACCOUNT)
        try:
            with factory.open_client(account.connection) as client:
                data = client.send_message(request.to, request.body)
        except AuthExpiredError as exc:
            raise PermanentAdapterError(AUTH_EXPIRED) from exc
        except RetryableProviderError as exc:
            raise RetryableAdapterError(UNAVAILABLE) from exc
        except ProviderError as exc:
            raise PermanentAdapterError(REJECTED) from exc
        return SendResult(provider_message_id=str(data.get("message_id") or data.get("id") or ""))
