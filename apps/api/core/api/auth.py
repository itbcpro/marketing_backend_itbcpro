"""Authentication classes for the Ninja API."""

from typing import Any

from django.http import HttpRequest
from ninja.security import APIKeyCookie, SessionAuth


class SessionCookieAuth(SessionAuth):
    """Authenticated Django session with CSRF enforcement on unsafe methods."""


class CsrfOnly(APIKeyCookie):
    """Require a valid CSRF cookie/header pair without requiring a logged-in user (login form)."""

    param_name = "csrftoken"

    def authenticate(self, request: HttpRequest, key: str | None) -> Any:
        return key or None


session_auth = SessionCookieAuth()
csrf_only = CsrfOnly()
