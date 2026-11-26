"""RFC 9457 style problem responses shared by the API layer and middleware."""

from http import HTTPStatus

import structlog
from django.http import HttpRequest, JsonResponse
from django.utils.translation import gettext_lazy

from core.error_messages import ERROR_MESSAGES
from core.errors import ErrorCode


def problem_response(
    request: HttpRequest,
    *,
    status: int,
    code: ErrorCode,
    detail: str = "",
    errors: list[dict[str, object]] | None = None,
) -> JsonResponse:
    message = detail or str(ERROR_MESSAGES.get(code, gettext_lazy("An unexpected error occurred.")))
    body: dict[str, object] = {
        "type": f"urn:itbcpro:problem:{code.value}",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code.value,
        "detail": message,
        "request_id": structlog.contextvars.get_contextvars().get("request_id", ""),
    }
    if errors:
        body["errors"] = errors
    response = JsonResponse(body, status=status)
    response["Content-Type"] = "application/problem+json"
    return response
