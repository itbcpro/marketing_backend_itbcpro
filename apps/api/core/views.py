"""Operational endpoints outside the versioned API."""

from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from core.health import run_checks


@require_GET
def liveness(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def readiness(request: HttpRequest) -> JsonResponse:
    checks = run_checks()
    healthy = all(value == "ok" for value in checks.values())
    return JsonResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status=200 if healthy else 503,
    )
