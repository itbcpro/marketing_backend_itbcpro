"""Public webhook ingress. The signature is the only credential."""

from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status
from ninja.throttling import BaseThrottle

from crm.common.throttles import WebhookThrottle
from crm.ops import metrics
from crm.webhooks import services

router = Router(tags=["crm-public"], auth=None)
THROTTLE: list[BaseThrottle] = [WebhookThrottle("600/m")]


@router.post(
    "/hooks/{public_id}",
    response={202: None},
    throttle=THROTTLE,
    operation_id="crm_public_webhook",
)
def receive(request: HttpRequest, public_id: str) -> Status[None]:
    with metrics.watch("webhooks"):
        services.receive(
            public_id, request.headers.get(services.SIGNATURE_HEADER, ""), request.body
        )
    return Status(202, None)
