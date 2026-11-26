"""Domain and provider error taxonomy.

Errors carry a stable machine code. Human text lives in the message catalog and is
translated at the API boundary, never at the raise site.
"""

from datetime import datetime
from enum import StrEnum


class ErrorCode(StrEnum):
    UNKNOWN = "unknown"
    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    UNAUTHENTICATED = "unauthenticated"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    BUDGET_EXCEEDED = "budget_exceeded"
    PLAN_LIMIT = "plan_limit"
    STRUCTURED_OUTPUT_FAILED = "structured_output_failed"
    ACTION_BLOCKED = "action_blocked"
    ACTION_STATE = "action_state"
    KILL_SWITCH = "kill_switch"
    MFA_REQUIRED = "mfa_required"
    INVALID_MFA_CODE = "invalid_mfa_code"
    ACCOUNT_LOCKED = "account_locked"
    INVALID_TOKEN = "invalid_token"
    WEAK_PASSWORD = "weak_password"
    WORKSPACE_REQUIRED = "workspace_required"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"
    INVALID_CREDENTIALS = "invalid_credentials"
    PROVIDER_AUTH_EXPIRED = "provider_auth_expired"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_POLICY_REJECTED = "provider_policy_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REJECTED = "provider_rejected"


class DomainError(Exception):
    code: ErrorCode = ErrorCode.UNKNOWN
    http_status: int = 400

    def __init__(
        self, detail: str = "", *, code: ErrorCode | None = None, **params: object
    ) -> None:
        super().__init__(detail or (code or self.code).value)
        if code is not None:
            self.code = code
        self.detail = detail
        self.params = params


class ValidationFailedError(DomainError):
    code = ErrorCode.VALIDATION_FAILED
    http_status = 422


class NotFoundError(DomainError):
    code = ErrorCode.NOT_FOUND
    http_status = 404


class PermissionDeniedError(DomainError):
    code = ErrorCode.PERMISSION_DENIED
    http_status = 403


class UnauthenticatedError(DomainError):
    code = ErrorCode.UNAUTHENTICATED
    http_status = 401


class ConflictError(DomainError):
    code = ErrorCode.CONFLICT
    http_status = 409


class ProviderError(DomainError):
    """Base class for failures talking to an external platform."""

    code = ErrorCode.PROVIDER_REJECTED
    http_status = 502

    def __init__(
        self,
        detail: str = "",
        *,
        provider: str = "",
        code: ErrorCode | None = None,
        **params: object,
    ) -> None:
        super().__init__(detail, code=code, provider=provider, **params)
        self.provider = provider


class RetryableProviderError(ProviderError):
    code = ErrorCode.PROVIDER_UNAVAILABLE
    http_status = 503


class PermanentProviderError(ProviderError):
    code = ErrorCode.PROVIDER_REJECTED


class RateLimitedError(RetryableProviderError):
    code = ErrorCode.PROVIDER_RATE_LIMITED
    http_status = 429

    def __init__(
        self, detail: str = "", *, provider: str = "", reset_at: datetime | None = None
    ) -> None:
        super().__init__(detail, provider=provider)
        self.reset_at = reset_at


class AuthExpiredError(ProviderError):
    code = ErrorCode.PROVIDER_AUTH_EXPIRED
    http_status = 401


class PolicyRejectedError(PermanentProviderError):
    code = ErrorCode.PROVIDER_POLICY_REJECTED
    http_status = 422
