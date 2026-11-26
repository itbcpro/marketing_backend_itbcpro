"""Public Twilio callbacks. The request signature is the only credential."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from ninja import Router
from ninja.throttling import BaseThrottle

from crm.common.throttles import WebhookThrottle
from crm.ops import metrics
from crm.sms import webhooks

router = Router(tags=["crm-public"], auth=None)
THROTTLE: list[BaseThrottle] = [WebhookThrottle("600/m")]
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@router.post(
    "/sms/twilio/{public_id}/{kind}",
    response={200: None},
    throttle=THROTTLE,
    operation_id="crm_public_sms_twilio",
)
def callback(request: HttpRequest, public_id: str, kind: str) -> HttpResponse:
    params = {key: request.POST[key] for key in request.POST}
    with metrics.watch("sms"):
        webhooks.receive(public_id, kind, request.headers.get("X-Twilio-Signature", ""), params)
    # Twilio expects TwiML back from the inbound hook; an empty one means "no automatic reply".
    return HttpResponse(EMPTY_TWIML, content_type="text/xml")
