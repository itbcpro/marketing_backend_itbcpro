"""Request authentication for the API: sessions (with MFA enrolment gate) and bearer tokens."""

from django.http import HttpRequest
from ninja.security import HttpBearer

from core.api.auth import SessionCookieAuth
from core.errors import DomainError, ErrorCode
from identity import jwt_service, mfa, tokens
from identity.models import User

AUTH_PATH_PREFIX = "/api/v1/auth/"


class MfaEnrollmentRequiredError(DomainError):
    code = ErrorCode.MFA_REQUIRED
    http_status = 403


class EnrolledSessionAuth(SessionCookieAuth):
    """Session auth that blocks everything except the auth endpoints until MFA is set up."""

    def authenticate(self, request: HttpRequest, key: str | None) -> User | None:
        user = super().authenticate(request, key)
        if (
            isinstance(user, User)
            and not request.path.startswith(AUTH_PATH_PREFIX)
            and mfa.requires_mfa_enrollment(user)
        ):
            raise MfaEnrollmentRequiredError()
        return user if isinstance(user, User) else None


class TokenAuth(HttpBearer):
    """Bearer API tokens for machine clients. Read-only tokens cannot use unsafe methods."""

    def authenticate(self, request: HttpRequest, token: str) -> User | None:
        found = tokens.authenticate(token, method=request.method or "GET")
        if found is None:
            return None
        request.user = found.user
        return found.user


class JwtAuth(HttpBearer):
    """Short-lived signed access tokens. The token is bound to exactly one workspace."""

    def authenticate(self, request: HttpRequest, token: str) -> User | None:
        try:
            claims, user = jwt_service.verify_access(token)
        except DomainError:
            return None
        request.user = user
        request.jwt_claims = claims  # type: ignore[attr-defined]
        return user


session_only = SessionCookieAuth()
api_auth = [EnrolledSessionAuth(), TokenAuth(), JwtAuth()]
