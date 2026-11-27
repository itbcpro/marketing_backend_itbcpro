"""Authentication use cases: sign-in with lockout and MFA, password reset and change."""

import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from django.conf import settings
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from audit.actors import Actor
from audit.services import record_event
from core.errors import DomainError, ErrorCode, UnauthenticatedError, ValidationFailedError
from identity import jwt_service, mfa, passkeys, sessions
from identity.models import PasskeyCredential, RecoveryCode, TotpDevice, User
from notifications.email import send_templated_email

BACKEND = "django.contrib.auth.backends.ModelBackend"
PENDING_USER_KEY = "pending_mfa_user"
PENDING_AT_KEY = "pending_mfa_at"
PENDING_TTL_SECONDS = 300
_DUMMY_HASH = make_password("unused-timing-equaliser")


class AccountLockedError(DomainError):
    code = ErrorCode.ACCOUNT_LOCKED
    http_status = 423


@dataclass(frozen=True, slots=True)
class LoginResult:
    status: Literal["ok", "mfa_required"]
    user: User | None = None


def _request_id(request: HttpRequest) -> str:
    return str(getattr(request, "request_id", ""))


def _lookup(email: str) -> User | None:
    return User.objects.filter(email=email.strip().lower()).first()


def _register_failure(user: User, request: HttpRequest) -> None:
    user.failed_login_count += 1
    fields = ["failed_login_count", "updated_at"]
    just_locked = user.failed_login_count >= settings.LOGIN_MAX_FAILURES
    if just_locked:
        user.locked_until = timezone.now() + timedelta(minutes=settings.LOGIN_LOCK_MINUTES)
        user.failed_login_count = 0
        fields += ["locked_until"]
    user.save(update_fields=fields)
    record_event(
        action="auth.account_locked" if just_locked else "auth.login_failed",
        actor=Actor.system("auth"),
        target_type="user",
        target_id=user.pk,
        request_id=_request_id(request),
    )
    if just_locked:
        send_templated_email(user.email, "account_locked", {"minutes": settings.LOGIN_LOCK_MINUTES})


def _reset_failures(user: User) -> None:
    if user.failed_login_count or user.locked_until:
        user.failed_login_count = 0
        user.locked_until = None
        user.save(update_fields=["failed_login_count", "locked_until", "updated_at"])


def _finish_login(request: HttpRequest, user: User) -> User:
    request.session.pop(PENDING_USER_KEY, None)
    request.session.pop(PENDING_AT_KEY, None)
    login(request, user, backend=BACKEND)
    _reset_failures(user)
    record_event(
        action="auth.login",
        actor=Actor.human(user.pk),
        target_type="user",
        target_id=user.pk,
        request_id=_request_id(request),
    )
    return user


def login_with_password(request: HttpRequest, *, email: str, password: str) -> LoginResult:
    user = _lookup(email)
    if user is None:
        check_password(password, _DUMMY_HASH)  # equalise timing with the real branch
        record_event(
            action="auth.login_failed",
            actor=Actor.system("auth"),
            after={"email": email.strip().lower()},
            request_id=_request_id(request),
        )
        raise UnauthenticatedError(code=ErrorCode.INVALID_CREDENTIALS)

    valid = user.is_active and user.check_password(password)
    if user.is_locked:
        # Reveal the lock only to someone who knows the password, so accounts cannot be probed.
        raise (
            AccountLockedError()
            if valid
            else UnauthenticatedError(code=ErrorCode.INVALID_CREDENTIALS)
        )
    if not valid:
        _register_failure(user, request)
        raise UnauthenticatedError(code=ErrorCode.INVALID_CREDENTIALS)

    if mfa.mfa_enabled(user):
        request.session.cycle_key()
        request.session[PENDING_USER_KEY] = str(user.pk)
        request.session[PENDING_AT_KEY] = int(time.time())
        return LoginResult(status="mfa_required")
    return LoginResult(status="ok", user=_finish_login(request, user))


def _pending_user(request: HttpRequest) -> User:
    raw = request.session.get(PENDING_USER_KEY)
    started = request.session.get(PENDING_AT_KEY, 0)
    if not raw or time.time() - started > PENDING_TTL_SECONDS:
        raise UnauthenticatedError(code=ErrorCode.UNAUTHENTICATED)
    user = User.objects.filter(pk=raw, is_active=True).first()
    if user is None:
        raise UnauthenticatedError(code=ErrorCode.UNAUTHENTICATED)
    return user


def complete_mfa_login(request: HttpRequest, *, code: str) -> User:
    """Second step: a TOTP code or an unused recovery code."""
    user = _pending_user(request)
    if user.is_locked:
        raise AccountLockedError()
    if mfa.verify_totp(user, code) or mfa.use_recovery_code(user, code):
        return _finish_login(request, user)
    _register_failure(user, request)
    raise ValidationFailedError(code=ErrorCode.INVALID_MFA_CODE)


def begin_passkey_login(request: HttpRequest) -> dict[str, object]:
    return passkeys.begin_authentication(request, _pending_user(request))


def complete_passkey_login(request: HttpRequest, credential: dict[str, object]) -> User:
    user = _pending_user(request)
    if user.is_locked:
        raise AccountLockedError()
    if passkeys.verify_authentication(request, user, credential):
        return _finish_login(request, user)
    _register_failure(user, request)
    raise ValidationFailedError(code=ErrorCode.INVALID_MFA_CODE)


def logout_user(request: HttpRequest) -> None:
    user = request.user
    if user.is_authenticated:
        record_event(
            action="auth.logout",
            actor=Actor.human(user.pk),
            target_type="user",
            target_id=user.pk,
            request_id=_request_id(request),
        )
        key = request.session.session_key
        logout(request)
        if key:
            from identity.models import SessionRecord

            SessionRecord.objects.filter(session_key=key).delete()
    else:
        logout(request)


# ---- password reset and change ----------------------------------------------------------------


def _validate_new_password(user: User, password: str) -> None:
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        raise ValidationFailedError("; ".join(exc.messages), code=ErrorCode.WEAK_PASSWORD) from exc


def request_password_reset(email: str) -> None:
    """Send a reset link if the account exists. Callers must respond identically either way."""
    user = _lookup(email)
    if user is None or not user.is_active:
        return
    uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
    token = default_token_generator.make_token(user)
    link = f"{settings.WEB_BASE_URL}/reset-password?uid={uid}&token={token}"
    send_templated_email(user.email, "password_reset", {"link": link})
    record_event(
        action="auth.password_reset_requested",
        actor=Actor.system("auth"),
        target_type="user",
        target_id=user.pk,
    )


def confirm_password_reset(*, uid: str, token: str, new_password: str) -> User:
    try:
        user = User.objects.filter(pk=force_str(urlsafe_base64_decode(uid))).first()
    except ValueError, TypeError, OverflowError:
        user = None
    if user is None or not default_token_generator.check_token(user, token):
        raise ValidationFailedError(code=ErrorCode.INVALID_TOKEN)
    _apply_new_password(user, new_password, action="auth.password_reset")
    return user


def change_password(request: HttpRequest, user: User, *, current: str, new: str) -> None:
    if not user.check_password(current):
        raise UnauthenticatedError(code=ErrorCode.INVALID_CREDENTIALS)
    _apply_new_password(user, new, action="auth.password_changed", keep=request.session.session_key)
    update_session_auth_hash(request, user)


def _apply_new_password(
    user: User, new_password: str, *, action: str, keep: str | None = None
) -> None:
    _validate_new_password(user, new_password)
    user.set_password(new_password)
    user.password_changed_at = timezone.now()
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None
    user.save()
    sessions.revoke_all_sessions(user, keep=keep)
    jwt_service.revoke_user(user)
    record_event(action=action, actor=Actor.human(user.pk), target_type="user", target_id=user.pk)


TEMP_PASSWORD_BYTES = 18
TEMP_PASSWORD_ATTEMPTS = 5


def new_temporary_password(user: User) -> str:
    """A random password that passes the password policy. The caller sets it and flags the user."""
    for _ in range(TEMP_PASSWORD_ATTEMPTS):
        candidate = secrets.token_urlsafe(TEMP_PASSWORD_BYTES)
        try:
            validate_password(candidate, user)
        except DjangoValidationError:
            continue
        return candidate
    raise ValidationFailedError("could not generate a password")  # pragma: no cover


def set_temporary_password(user: User, *, by: User) -> str:
    """Give the user a new random password they must change; every session of theirs ends."""
    password = new_temporary_password(user)
    with transaction.atomic():
        user.set_password(password)
        user.must_change_password = True
        user.password_changed_at = None
        user.failed_login_count = 0
        user.locked_until = None
        user.save()
        sessions.revoke_all_sessions(user)
        jwt_service.revoke_user(user)
        record_event(
            action="auth.password_reset_by_admin",
            actor=Actor.human(by.pk),
            target_type="user",
            target_id=user.pk,
        )
    return password


def has_second_factor(user: User) -> bool:
    return mfa.mfa_enabled(user)


def reset_mfa(user: User, *, by: User) -> None:
    """Remove every second factor and sign the user out everywhere (an administrator's action).

    The next sign-in works with the password alone; where the user's role requires MFA they are
    sent through enrolment again before anything else is reachable.
    """
    with transaction.atomic():
        TotpDevice.objects.filter(user=user).delete()
        RecoveryCode.objects.filter(user=user).delete()
        PasskeyCredential.objects.filter(user=user).delete()
        sessions.revoke_all_sessions(user)
        jwt_service.revoke_user(user)
        record_event(
            action="mfa.reset_by_admin",
            actor=Actor.human(by.pk),
            target_type="user",
            target_id=user.pk,
        )
