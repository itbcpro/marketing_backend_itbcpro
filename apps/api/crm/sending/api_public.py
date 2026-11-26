"""Public, unauthenticated endpoints. The signed link is the only credential."""

from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Schema, Status
from ninja.throttling import BaseThrottle

from crm.common.throttles import PublicLinkThrottle
from crm.sending import unsubscribe

router = Router(tags=["crm-public"], auth=None)
THROTTLE: list[BaseThrottle] = [PublicLinkThrottle("30/m")]


class UnsubscribeOut(Schema):
    address: str
    unsubscribed: bool


@router.get(
    "/unsubscribe/{token}",
    response=UnsubscribeOut,
    throttle=THROTTLE,
    operation_id="crm_public_unsubscribe_info",
)
def info(request: HttpRequest, token: str) -> UnsubscribeOut:
    """Show who the link is for, without changing anything (link scanners open links)."""
    target = unsubscribe.read_token(token)
    return UnsubscribeOut(
        address=unsubscribe.mask(unsubscribe.current_address(target)),
        unsubscribed=unsubscribe.is_unsubscribed(target),
    )


@router.post(
    "/unsubscribe/{token}",
    response={204: None},
    throttle=THROTTLE,
    operation_id="crm_public_unsubscribe",
)
def confirm(request: HttpRequest, token: str) -> Status[None]:
    """One-click unsubscribe (RFC 8058): a POST from the mail client or the web page."""
    unsubscribe.unsubscribe(unsubscribe.read_token(token))
    return Status(204, None)
