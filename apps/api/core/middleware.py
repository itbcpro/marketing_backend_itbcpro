"""Request correlation and idempotency middleware."""

import hashlib
import uuid
from collections.abc import Callable
from http import HTTPStatus

import structlog
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse

from core import tenancy
from core.api.problem import problem_response
from core.errors import ErrorCode
from core.ids import new_id
from core.models import IdempotencyRecord

REQUEST_ID_HEADER = "X-Request-Id"
IDEMPOTENCY_HEADER = "Idempotency-Key"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_IDEMPOTENCY_BODY_BYTES = 1_000_000
MAX_REQUEST_ID_LENGTH = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 200
MAX_STORED_PATH_LENGTH = 500


class RequestIdMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = (
            incoming if incoming and len(incoming) <= MAX_REQUEST_ID_LENGTH else new_id().hex
        )
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.request_id = request_id  # type: ignore[attr-defined]
        response = self.get_response(request)
        response[REQUEST_ID_HEADER] = request_id
        return response


class TenantContextMiddleware:
    """Guarantee that no workspace context leaks between requests on a pooled connection."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        tenancy.clear()
        try:
            return self.get_response(request)
        finally:
            tenancy.clear()


class BodySizeLimitMiddleware:
    """Reject oversized bodies from the Content-Length header before anything reads them."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        declared = request.headers.get("Content-Length", "")
        limit = (
            settings.API_MAX_UPLOAD_BYTES
            if request.path.startswith(tuple(settings.API_UPLOAD_PATHS))
            else settings.API_MAX_BODY_BYTES
        )
        if declared.isdigit() and int(declared) > limit:
            return problem_response(
                request,
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                code=ErrorCode.PAYLOAD_TOO_LARGE,
            )
        return self.get_response(request)


class IdempotencyMiddleware:
    """Replay the stored response when an authenticated client retries with the same key."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        key = request.headers.get(IDEMPOTENCY_HEADER, "")
        user = getattr(request, "user", None)
        if not key or request.method not in UNSAFE_METHODS or not (user and user.is_authenticated):
            return self.get_response(request)
        if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH or len(request.body) > MAX_IDEMPOTENCY_BODY_BYTES:
            return problem_response(request, status=422, code=ErrorCode.VALIDATION_FAILED)

        digest = hashlib.sha256(
            f"{request.method}\n{request.get_full_path()}\n".encode() + request.body
        ).hexdigest()
        record, created = self._claim(user.pk, key, request, digest)
        if not created:
            return self._replay(request, record, digest)

        try:
            response = self.get_response(request)
        except BaseException:
            record.delete()  # never leave a key stuck in progress after a crash
            raise
        self._finish(record, response)
        return response

    @staticmethod
    def _claim(
        user_id: uuid.UUID, key: str, request: HttpRequest, digest: str
    ) -> tuple[IdempotencyRecord, bool]:
        try:
            with transaction.atomic():
                return (
                    IdempotencyRecord.objects.create(
                        user_id=user_id,
                        key=key,
                        method=request.method or "",
                        path=request.path[:MAX_STORED_PATH_LENGTH],
                        request_hash=digest,
                    ),
                    True,
                )
        except IntegrityError:
            return IdempotencyRecord.objects.get(user_id=user_id, key=key), False

    @staticmethod
    def _replay(request: HttpRequest, record: IdempotencyRecord, digest: str) -> HttpResponse:
        if record.request_hash != digest:
            return problem_response(request, status=422, code=ErrorCode.IDEMPOTENCY_KEY_REUSED)
        if record.state != IdempotencyRecord.State.COMPLETED:
            return problem_response(request, status=409, code=ErrorCode.IDEMPOTENCY_IN_PROGRESS)
        replay = HttpResponse(
            bytes(record.response_body or b""),
            status=record.response_status or 200,
            content_type=record.response_content_type or "application/json",
        )
        replay["Idempotent-Replayed"] = "true"
        return replay

    @staticmethod
    def _finish(record: IdempotencyRecord, response: HttpResponse) -> None:
        if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR or getattr(
            response, "streaming", False
        ):
            record.delete()
            return
        record.state = IdempotencyRecord.State.COMPLETED
        record.response_status = response.status_code
        record.response_body = response.content
        record.response_content_type = response.headers.get("Content-Type", "")
        record.save(
            update_fields=[
                "state",
                "response_status",
                "response_body",
                "response_content_type",
                "updated_at",
            ]
        )
