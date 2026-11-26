"""The pixel and the link redirect. The signed token is the only credential."""

from __future__ import annotations

import contextlib

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from ninja import Router
from ninja.throttling import BaseThrottle

from core.errors import NotFoundError
from crm.common.throttles import TrackingThrottle
from crm.tracking import services

router = Router(tags=["crm-public"], auth=None)
THROTTLE: list[BaseThrottle] = [TrackingThrottle("1200/m")]
# The smallest transparent GIF there is.
PIXEL = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
NO_STORE = "no-store, max-age=0"


@router.get(
    "/t/o/{token}", response={200: None}, throttle=THROTTLE, operation_id="crm_public_track_open"
)
def open_pixel(request: HttpRequest, token: str) -> HttpResponse:
    # A broken or old link still gets the image, so mail apps show nothing odd.
    with contextlib.suppress(NotFoundError):
        services.record_open(token)
    response = HttpResponse(PIXEL, content_type="image/gif")
    response["Cache-Control"] = NO_STORE
    return response


@router.get(
    "/t/c/{token}", response={302: None}, throttle=THROTTLE, operation_id="crm_public_track_click"
)
def click(request: HttpRequest, token: str) -> HttpResponseRedirect:
    target = services.record_click(token)
    response = HttpResponseRedirect(target)
    response["Cache-Control"] = NO_STORE
    response["Referrer-Policy"] = "no-referrer"
    return response
