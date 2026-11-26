"""Conditional GET support for cacheable read endpoints."""

import hashlib
import json
from http import HTTPStatus
from typing import Any

from django.http import HttpRequest, HttpResponse


def etag_response(request: HttpRequest, payload: Any) -> HttpResponse:
    """Return the JSON payload with a strong ETag, or 304 when the client already has it."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    candidates = {part.strip() for part in request.headers.get("If-None-Match", "").split(",")}
    if etag in candidates or "*" in candidates:
        response = HttpResponse(status=HTTPStatus.NOT_MODIFIED)
    else:
        response = HttpResponse(body, content_type="application/json")
    response["ETag"] = etag
    response["Cache-Control"] = "private, must-revalidate"
    return response
