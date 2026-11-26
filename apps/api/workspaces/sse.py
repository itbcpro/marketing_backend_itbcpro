"""Authenticated SSE endpoint for workspace events."""

import uuid
from http import HTTPStatus

from asgiref.sync import sync_to_async
from django.http import HttpRequest, StreamingHttpResponse
from django.http.response import HttpResponseBase

from core import sse
from core.api.problem import problem_response
from core.errors import DomainError, ErrorCode
from identity.models import User
from workspaces.permissions import Permission
from workspaces.services import require_permission


async def workspace_events(request: HttpRequest) -> HttpResponseBase:
    """GET /api/v1/events/stream?workspace_id=<uuid>  (EventSource cannot send custom headers)."""
    user = await request.auser()
    if not isinstance(user, User) or not user.is_authenticated:
        return problem_response(
            request, status=HTTPStatus.UNAUTHORIZED, code=ErrorCode.UNAUTHENTICATED
        )
    try:
        workspace_id = uuid.UUID(request.GET.get("workspace_id", ""))
        await sync_to_async(require_permission)(user, workspace_id, Permission.VIEW)
    except ValueError:
        return problem_response(
            request, status=HTTPStatus.UNPROCESSABLE_ENTITY, code=ErrorCode.WORKSPACE_REQUIRED
        )
    except DomainError:
        return problem_response(
            request, status=HTTPStatus.UNPROCESSABLE_ENTITY, code=ErrorCode.WORKSPACE_REQUIRED
        )

    response = StreamingHttpResponse(
        sse.stream(
            sse.workspace_channel(workspace_id),
            last_event_id=request.headers.get("Last-Event-ID") or request.GET.get("last_event_id"),
        ),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
