"""Translatable messages for every error code. English source text; translations live in locale/."""

from django.utils.translation import gettext_lazy as _

from core.errors import ErrorCode

ERROR_MESSAGES = {
    ErrorCode.UNKNOWN: _("An unexpected error occurred."),
    ErrorCode.VALIDATION_FAILED: _("The submitted data is not valid."),
    ErrorCode.NOT_FOUND: _("The requested resource was not found."),
    ErrorCode.PERMISSION_DENIED: _("You do not have permission to perform this action."),
    ErrorCode.UNAUTHENTICATED: _("Authentication is required."),
    ErrorCode.CONFLICT: _("The request conflicts with the current state."),
    ErrorCode.RATE_LIMITED: _("Too many requests. Please try again later."),
    ErrorCode.PAYLOAD_TOO_LARGE: _("The request body is too large."),
    ErrorCode.BUDGET_EXCEEDED: _("The daily AI budget has been reached."),
    ErrorCode.PLAN_LIMIT: _("Your plan does not allow more of this. Upgrade to continue."),
    ErrorCode.STRUCTURED_OUTPUT_FAILED: _("The AI response could not be understood."),
    ErrorCode.ACTION_BLOCKED: _("A safety rule blocked this action."),
    ErrorCode.ACTION_STATE: _("This action cannot be changed in its current state."),
    ErrorCode.KILL_SWITCH: _("All external changes are paused."),
    ErrorCode.MFA_REQUIRED: _("A second authentication factor is required."),
    ErrorCode.INVALID_MFA_CODE: _("The verification code is not valid."),
    ErrorCode.ACCOUNT_LOCKED: _("Too many failed attempts. The account is temporarily locked."),
    ErrorCode.INVALID_TOKEN: _("The link or token is invalid or has expired."),
    ErrorCode.WEAK_PASSWORD: _("The password does not meet the security requirements."),
    ErrorCode.WORKSPACE_REQUIRED: _("A valid workspace must be selected."),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: _(
        "This idempotency key was already used with a different request."
    ),
    ErrorCode.IDEMPOTENCY_IN_PROGRESS: _(
        "A request with this idempotency key is still being processed."
    ),
    ErrorCode.INVALID_CREDENTIALS: _("The email or password is incorrect."),
    ErrorCode.PROVIDER_AUTH_EXPIRED: _("The connection to the external platform has expired."),
    ErrorCode.PROVIDER_RATE_LIMITED: _("The external platform is rate limiting requests."),
    ErrorCode.PROVIDER_POLICY_REJECTED: _(
        "The external platform rejected the content for policy reasons."
    ),
    ErrorCode.PROVIDER_UNAVAILABLE: _("The external platform is temporarily unavailable."),
    ErrorCode.PROVIDER_REJECTED: _("The external platform rejected the request."),
}
