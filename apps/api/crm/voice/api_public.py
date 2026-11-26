"""Public Twilio voice callbacks. The request signature is the only credential."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from ninja import Router
from ninja.throttling import BaseThrottle

from crm.common.throttles import WebhookThrottle
from crm.ops import metrics
from crm.voice import webhooks

router = Router(tags=["crm-public"], auth=None)
THROTTLE: list[BaseThrottle] = [WebhookThrottle("600/m")]


@router.post(
    "/voice/twilio/{public_id}/{kind}",
    response={200: None},
    throttle=THROTTLE,
    operation_id="crm_public_voice_twilio",
)
def callback(request: HttpRequest, public_id: str, kind: str) -> HttpResponse:
    params = {key: request.POST[key] for key in request.POST}
    with metrics.watch("voice"):
        document = webhooks.receive(
            public_id, kind, request.headers.get("X-Twilio-Signature", ""), params
        )
    return HttpResponse(document, content_type="text/xml")
