"""Plain Django views of the identity app."""

from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from identity.jwt_keys import get_keyring

JWKS_MAX_AGE_SECONDS = 300


@require_GET
@cache_control(public=True, max_age=JWKS_MAX_AGE_SECONDS)
def jwks(request: HttpRequest) -> JsonResponse:
    """Public verification keys (no secrets), cacheable by gateways and other services."""
    return JsonResponse(get_keyring().jwks())
