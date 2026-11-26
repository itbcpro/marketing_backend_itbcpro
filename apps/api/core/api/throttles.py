"""Throttles keyed by the caller's address, whatever the request is authenticated with.

``AnonRateThrottle`` skips any request that carries an auth object. Endpoints protected by
``csrf_only`` (sign-in, password reset, sign-up) do carry one, the CSRF cookie, so an anonymous
throttle would never limit them. These throttles count by address alone, each in its own scope
so one limit cannot use up another's allowance.
"""

from django.http import HttpRequest
from ninja.throttling import SimpleRateThrottle


class AddressThrottle(SimpleRateThrottle):
    scope = "address"

    def get_cache_key(self, request: HttpRequest) -> str | None:
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class LoginThrottle(AddressThrottle):
    """Sign-in, password reset and the other pre-login forms."""

    scope = "login"


class SignupThrottle(AddressThrottle):
    scope = "signup"
