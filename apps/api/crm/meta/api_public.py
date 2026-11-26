"""Public Meta webhook address. The verify token and the body signature are the credentials."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from ninja import Query, Router, Schema
from ninja.throttling import BaseThrottle
from pydantic import Field

from crm.common.throttles import WebhookThrottle
from crm.meta import webhooks
from crm.ops import metrics

router = Router(tags=["crm-public"], auth=None)
THROTTLE: list[BaseThrottle] = [WebhookThrottle("600/m")]


class Challenge(Schema):
    mode: str = Field("", alias="hub.mode")
    token: str = Field("", alias="hub.verify_token")
    challenge: str = Field("", alias="hub.challenge")


@router.get(
    "/meta/{public_id}",
    response={200: None},
    throttle=THROTTLE,
    operation_id="crm_public_meta_verify",
)
def verify(request: HttpRequest, public_id: str, params: Query[Challenge]) -> HttpResponse:
    with metrics.watch("meta_verify"):
        answer = webhooks.verify_challenge(public_id, params.mode, params.token, params.challenge)
    return HttpResponse(answer, content_type="text/plain")


@router.post(
    "/meta/{public_id}", response={200: None}, throttle=THROTTLE, operation_id="crm_public_meta"
)
def receive(request: HttpRequest, public_id: str) -> HttpResponse:
    with metrics.watch("meta"):
        webhooks.receive(public_id, request.headers.get("X-Hub-Signature-256", ""), request.body)
    return HttpResponse("ok", content_type="text/plain")
