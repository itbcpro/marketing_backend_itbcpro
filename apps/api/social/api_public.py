"""Endpoints Instagram itself calls: the webhook and short-lived media links. No session needed."""

import json
from typing import Any

from django.http import FileResponse, HttpRequest, HttpResponse
from ninja import Router
from ninja.throttling import AnonRateThrottle

from core.errors import NotFoundError
from media.storage import get_storage
from social import public_media
from social.instagram import webhooks

router = Router(tags=["instagram-public"], auth=None)

# Instagram calls these from a handful of addresses in bursts; deliveries are signed and links are
# signed, so the default anonymous limit (60 a minute) would only drop real events.
PUBLIC_LIMIT = [AnonRateThrottle("600/m")]

MAX_BODY = 1_000_000


@router.get(
    "/instagram/webhook",
    auth=None,
    throttle=PUBLIC_LIMIT,
    operation_id="instagram_webhook_verify",
    response={200: None},
)
def verify_webhook(request: HttpRequest) -> HttpResponse:
    """Instagram's subscription handshake: echo the challenge when the verify token matches."""
    answer = webhooks.verify_challenge(
        request.GET.get("hub.mode", ""),
        request.GET.get("hub.verify_token", ""),
        request.GET.get("hub.challenge", ""),
    )
    if answer is None:
        return HttpResponse(status=403)
    return HttpResponse(answer, content_type="text/plain")


@router.post(
    "/instagram/webhook",
    auth=None,
    throttle=PUBLIC_LIMIT,
    operation_id="instagram_webhook_receive",
    response={200: None},
)
def receive_webhook(request: HttpRequest) -> HttpResponse:
    """Signed deliveries. A wrong signature is refused before the body is parsed."""
    body = request.body
    if len(body) > MAX_BODY or not webhooks.verify_signature(
        body, request.headers.get("X-Hub-Signature-256", "")
    ):
        return HttpResponse(status=403)
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return HttpResponse(status=400)
    if isinstance(payload, dict):
        webhooks.ingest(payload)
    return HttpResponse("ok", content_type="text/plain")


@router.get(
    "/public-media/{token}",
    auth=None,
    throttle=PUBLIC_LIMIT,
    operation_id="instagram_public_media",
    response={200: None},
)
def public_media_file(request: HttpRequest, token: str) -> FileResponse:
    """The file behind a signed link (Instagram downloads post media from here)."""
    asset = public_media.resolve_token(token)
    try:
        handle = get_storage().open(asset.storage_key)
    except FileNotFoundError as exc:
        raise NotFoundError("file not found") from exc
    response = FileResponse(handle, content_type=asset.mime_type or "application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=600"
    return response
