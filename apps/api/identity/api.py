"""Authentication endpoints (session based; the Next.js BFF holds the cookie)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from django.http import HttpRequest
from django.middleware.csrf import get_token
from ninja import Router, Schema, Status
from ninja.throttling import BaseThrottle

from core.api.auth import csrf_only, session_auth
from core.api.throttles import LoginThrottle, SignupThrottle
from core.errors import ErrorCode, UnauthenticatedError
from identity import jwt_service, mfa, passkeys, services, sessions, signup, tokens
from identity.authentication import MfaEnrollmentRequiredError, api_auth
from identity.models import User
from workspaces import invitations
from workspaces.models import Membership
from workspaces.selectors import workspaces_for_user

router = Router(tags=["auth"])
LOGIN_THROTTLE: list[BaseThrottle] = [LoginThrottle("10/m")]


SIGNUP_THROTTLE: list[BaseThrottle] = [SignupThrottle("5/m")]


class LoginIn(Schema):
    email: str
    password: str


class MembershipOut(Schema):
    workspace_id: str
    workspace_name: str
    workspace_slug: str
    role: str


class MeOut(Schema):
    id: str
    email: str
    full_name: str
    locale: str
    time_zone: str
    mfa_enabled: bool
    mfa_enrollment_required: bool
    is_platform_admin: bool
    must_change_password: bool
    memberships: list[MembershipOut]


class LoginOut(Schema):
    status: Literal["ok", "mfa_required"]
    me: MeOut | None = None


class CsrfOut(Schema):
    csrf_token: str


class CodeIn(Schema):
    code: str


class PasswordIn(Schema):
    password: str


class SignupIn(Schema):
    email: str
    password: str
    full_name: str = ""
    organization_name: str


class VerifyEmailIn(Schema):
    uid: str
    token: str


class PasswordResetRequestIn(Schema):
    email: str


class PasswordResetConfirmIn(Schema):
    uid: str
    token: str
    new_password: str


class PasswordChangeIn(Schema):
    current_password: str
    new_password: str


class TotpBeginOut(Schema):
    secret: str
    uri: str


class RecoveryCodesOut(Schema):
    recovery_codes: list[str]


class MfaStatusOut(Schema):
    totp_enabled: bool
    passkeys: int
    recovery_codes_remaining: int


class PasskeyCredentialIn(Schema):
    credential: dict[str, Any]
    name: str = "Passkey"


class PasskeyOut(Schema):
    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None


class SessionOut(Schema):
    id: uuid.UUID
    user_agent: str
    ip_address: str | None
    last_seen_at: datetime
    current: bool


class TokenCreateIn(Schema):
    name: str
    scopes: list[str] = ["read"]
    expires_in_days: int | None = None


class TokenOut(Schema):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenCreatedOut(Schema):
    token: TokenOut
    secret: str


class InvitationAcceptIn(Schema):
    token: str
    password: str | None = None
    full_name: str = ""


def _me(user: User) -> MeOut:
    return MeOut(
        id=str(user.pk),
        email=user.email,
        full_name=user.full_name,
        locale=user.locale,
        time_zone=user.time_zone,
        mfa_enabled=mfa.mfa_enabled(user),
        mfa_enrollment_required=mfa.requires_mfa_enrollment(user),
        is_platform_admin=user.is_superuser and user.is_active,
        must_change_password=user.must_change_password,
        memberships=[
            MembershipOut(
                workspace_id=str(m.workspace_id),
                workspace_name=m.workspace.name,
                workspace_slug=m.workspace.slug,
                role=m.role,
            )
            for m in workspaces_for_user(user.pk)
        ],
    )


def _user(request: HttpRequest) -> User:
    user = request.auth  # type: ignore[attr-defined]
    if not isinstance(user, User):
        raise UnauthenticatedError()
    return user


# ---- sign-in ----------------------------------------------------------------------------------


@router.get("/csrf", auth=None, response=CsrfOut, operation_id="auth_csrf")
def csrf(request: HttpRequest) -> CsrfOut:
    return CsrfOut(csrf_token=get_token(request))


@router.post(
    "/login", auth=csrf_only, throttle=LOGIN_THROTTLE, response=LoginOut, operation_id="auth_login"
)
def login_view(request: HttpRequest, payload: LoginIn) -> LoginOut:
    result = services.login_with_password(request, email=payload.email, password=payload.password)
    return LoginOut(status=result.status, me=_me(result.user) if result.user else None)


@router.post(
    "/login/mfa",
    auth=csrf_only,
    throttle=LOGIN_THROTTLE,
    response=LoginOut,
    operation_id="auth_login_mfa",
)
def login_mfa(request: HttpRequest, payload: CodeIn) -> LoginOut:
    user = services.complete_mfa_login(request, code=payload.code)
    return LoginOut(status="ok", me=_me(user))


@router.post(
    "/login/passkey/options",
    auth=csrf_only,
    throttle=LOGIN_THROTTLE,
    response=dict[str, Any],
    operation_id="auth_login_passkey_options",
)
def login_passkey_options(request: HttpRequest) -> dict[str, Any]:
    return services.begin_passkey_login(request)


@router.post(
    "/login/passkey/verify",
    auth=csrf_only,
    throttle=LOGIN_THROTTLE,
    response=LoginOut,
    operation_id="auth_login_passkey_verify",
)
def login_passkey_verify(request: HttpRequest, payload: PasskeyCredentialIn) -> LoginOut:
    user = services.complete_passkey_login(request, payload.credential)
    return LoginOut(status="ok", me=_me(user))


@router.post("/logout", auth=session_auth, response={204: None}, operation_id="auth_logout")
def logout_view(request: HttpRequest) -> Status[None]:
    services.logout_user(request)
    return Status(204, None)


@router.get("/me", auth=session_auth, response=MeOut, operation_id="auth_me")
def me(request: HttpRequest) -> MeOut:
    return _me(_user(request))


# ---- JWT --------------------------------------------------------------------------------------


class TokenIn(Schema):
    workspace_id: uuid.UUID


class RefreshIn(Schema):
    refresh_token: str


class TokenPairOut(Schema):
    access_token: str
    token_type: str = jwt_service.TOKEN_TYPE
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    workspace_id: uuid.UUID
    role: str


def _pair_out(pair: jwt_service.TokenPair) -> TokenPairOut:
    return TokenPairOut(
        access_token=pair.access_token,
        expires_in=pair.expires_in,
        refresh_token=pair.refresh_token,
        refresh_expires_in=pair.refresh_expires_in,
        workspace_id=pair.workspace_id,
        role=pair.role,
    )


def _client_meta(request: HttpRequest) -> tuple[str, str | None]:
    return request.headers.get("User-Agent", ""), request.META.get("REMOTE_ADDR")


@router.post(
    "/token",
    auth=session_auth,
    throttle=LOGIN_THROTTLE,
    response=TokenPairOut,
    operation_id="auth_token",
)
def token_issue(request: HttpRequest, payload: TokenIn) -> TokenPairOut:
    """Exchange the logged-in session (password and MFA already done) for a workspace token pair."""
    user = _user(request)
    if mfa.requires_mfa_enrollment(user):
        raise MfaEnrollmentRequiredError()
    membership = Membership.objects.filter(
        user=user, workspace_id=payload.workspace_id, workspace__is_active=True
    ).first()
    if membership is None:
        raise UnauthenticatedError(code=ErrorCode.WORKSPACE_REQUIRED)
    agent, ip = _client_meta(request)
    return _pair_out(jwt_service.issue_pair(user, membership, user_agent=agent, ip=ip))


@router.post(
    "/refresh",
    auth=None,
    throttle=LOGIN_THROTTLE,
    response=TokenPairOut,
    operation_id="auth_refresh",
)
def token_refresh(request: HttpRequest, payload: RefreshIn) -> TokenPairOut:
    agent, ip = _client_meta(request)
    return _pair_out(jwt_service.rotate(payload.refresh_token, user_agent=agent, ip=ip))


@router.post(
    "/token/revoke",
    auth=None,
    throttle=LOGIN_THROTTLE,
    response={204: None},
    operation_id="auth_refresh_revoke",
)
def token_revoke(request: HttpRequest, payload: RefreshIn) -> Status[None]:
    jwt_service.revoke_by_refresh(payload.refresh_token)
    return Status(204, None)


# ---- sign-up ----------------------------------------------------------------------------------


@router.post(
    "/signup",
    auth=csrf_only,
    throttle=SIGNUP_THROTTLE,
    response={202: None},
    operation_id="auth_signup",
)
def sign_up(request: HttpRequest, payload: SignupIn) -> Status[None]:
    signup.sign_up(**payload.model_dump())
    return Status(202, None)  # the same answer whether or not the address was already known


@router.post(
    "/verify-email",
    auth=csrf_only,
    throttle=LOGIN_THROTTLE,
    response={204: None},
    operation_id="auth_verify_email",
)
def verify_email(request: HttpRequest, payload: VerifyEmailIn) -> Status[None]:
    signup.verify_email(uid=payload.uid, token=payload.token)
    return Status(204, None)


# ---- password ---------------------------------------------------------------------------------


@router.post(
    "/password/reset-request",
    auth=csrf_only,
    throttle=LOGIN_THROTTLE,
    response={202: None},
    operation_id="auth_password_reset_request",
)
def password_reset_request(request: HttpRequest, payload: PasswordResetRequestIn) -> Status[None]:
    services.request_password_reset(payload.email)
    return Status(202, None)  # identical response whether or not the account exists


@router.post(
    "/password/reset-confirm",
    auth=csrf_only,
    throttle=LOGIN_THROTTLE,
    response={204: None},
    operation_id="auth_password_reset_confirm",
)
def password_reset_confirm(request: HttpRequest, payload: PasswordResetConfirmIn) -> Status[None]:
    services.confirm_password_reset(
        uid=payload.uid, token=payload.token, new_password=payload.new_password
    )
    return Status(204, None)


@router.post(
    "/password/change", auth=session_auth, response={204: None}, operation_id="auth_password_change"
)
def password_change(request: HttpRequest, payload: PasswordChangeIn) -> Status[None]:
    services.change_password(
        request, _user(request), current=payload.current_password, new=payload.new_password
    )
    return Status(204, None)


# ---- second factors ---------------------------------------------------------------------------


@router.get("/mfa", auth=session_auth, response=MfaStatusOut, operation_id="auth_mfa_status")
def mfa_status(request: HttpRequest) -> MfaStatusOut:
    user = _user(request)
    return MfaStatusOut(
        totp_enabled=mfa.has_confirmed_totp(user),
        passkeys=user.passkeys.count(),
        recovery_codes_remaining=mfa.remaining_recovery_codes(user),
    )


@router.post(
    "/mfa/totp/begin", auth=session_auth, response=TotpBeginOut, operation_id="auth_totp_begin"
)
def totp_begin(request: HttpRequest) -> TotpBeginOut:
    secret, uri = mfa.begin_totp_enrollment(_user(request))
    return TotpBeginOut(secret=secret, uri=uri)


@router.post(
    "/mfa/totp/confirm",
    auth=session_auth,
    response=RecoveryCodesOut,
    operation_id="auth_totp_confirm",
)
def totp_confirm(request: HttpRequest, payload: CodeIn) -> RecoveryCodesOut:
    return RecoveryCodesOut(
        recovery_codes=mfa.confirm_totp_enrollment(_user(request), payload.code)
    )


@router.post(
    "/mfa/totp/disable", auth=session_auth, response={204: None}, operation_id="auth_totp_disable"
)
def totp_disable(request: HttpRequest, payload: PasswordIn) -> Status[None]:
    user = _user(request)
    if not user.check_password(payload.password):
        raise UnauthenticatedError(code=ErrorCode.INVALID_CREDENTIALS)
    mfa.disable_totp(user)
    return Status(204, None)


@router.post(
    "/mfa/recovery-codes",
    auth=session_auth,
    response=RecoveryCodesOut,
    operation_id="auth_recovery_codes",
)
def regenerate_recovery_codes(request: HttpRequest, payload: PasswordIn) -> RecoveryCodesOut:
    user = _user(request)
    if not user.check_password(payload.password):
        raise UnauthenticatedError(code=ErrorCode.INVALID_CREDENTIALS)
    return RecoveryCodesOut(recovery_codes=mfa.generate_recovery_codes(user))


@router.post(
    "/mfa/passkeys/options",
    auth=session_auth,
    response=dict[str, Any],
    operation_id="auth_passkey_register_options",
)
def passkey_register_options(request: HttpRequest) -> dict[str, Any]:
    return passkeys.begin_registration(request, _user(request))


@router.post(
    "/mfa/passkeys", auth=session_auth, response=PasskeyOut, operation_id="auth_passkey_register"
)
def passkey_register(request: HttpRequest, payload: PasskeyCredentialIn) -> Any:
    return passkeys.finish_registration(request, _user(request), payload.credential, payload.name)


@router.get(
    "/mfa/passkeys", auth=session_auth, response=list[PasskeyOut], operation_id="auth_passkey_list"
)
def passkey_list(request: HttpRequest) -> Any:
    return list(_user(request).passkeys.order_by("created_at"))


@router.delete(
    "/mfa/passkeys/{passkey_id}",
    auth=session_auth,
    response={204: None},
    operation_id="auth_passkey_delete",
)
def passkey_delete(request: HttpRequest, passkey_id: uuid.UUID) -> Status[None]:
    passkeys.remove_passkey(_user(request), passkey_id)
    return Status(204, None)


# ---- sessions and tokens ----------------------------------------------------------------------


@router.get("/sessions", auth=session_auth, response=list[SessionOut], operation_id="auth_sessions")
def list_sessions(request: HttpRequest) -> list[SessionOut]:
    current = request.session.session_key
    return [
        SessionOut(
            id=r.pk,
            user_agent=r.user_agent,
            ip_address=r.ip_address,
            last_seen_at=r.last_seen_at,
            current=r.session_key == current,
        )
        for r in sessions.list_sessions(_user(request))
    ]


@router.post(
    "/sessions/revoke-others",
    auth=session_auth,
    response={204: None},
    operation_id="auth_sessions_revoke_others",
)
def revoke_other_sessions(request: HttpRequest) -> Status[None]:
    sessions.revoke_all_sessions(_user(request), keep=request.session.session_key)
    return Status(204, None)


@router.delete(
    "/sessions/{session_id}",
    auth=session_auth,
    response={204: None},
    operation_id="auth_session_revoke",
)
def revoke_session(request: HttpRequest, session_id: uuid.UUID) -> Status[None]:
    sessions.revoke_session(_user(request), session_id)
    return Status(204, None)


@router.get("/tokens", auth=api_auth, response=list[TokenOut], operation_id="auth_tokens")
def list_tokens(request: HttpRequest) -> Any:
    return list(_user(request).api_tokens.order_by("-created_at"))


@router.post(
    "/tokens", auth=session_auth, response=TokenCreatedOut, operation_id="auth_token_create"
)
def create_token(request: HttpRequest, payload: TokenCreateIn) -> TokenCreatedOut:
    token, secret = tokens.create_token(
        _user(request),
        name=payload.name,
        scopes=payload.scopes,
        expires_in_days=payload.expires_in_days,
    )
    return TokenCreatedOut(
        token=TokenOut.model_validate(token, from_attributes=True), secret=secret
    )


@router.delete(
    "/tokens/{token_id}",
    auth=session_auth,
    response={204: None},
    operation_id="auth_token_revoke",
)
def revoke_token(request: HttpRequest, token_id: uuid.UUID) -> Status[None]:
    tokens.revoke_token(_user(request), token_id)
    return Status(204, None)


# ---- invitations ------------------------------------------------------------------------------


@router.post(
    "/invitations/accept",
    auth=csrf_only,
    throttle=LOGIN_THROTTLE,
    response={204: None},
    operation_id="auth_invitation_accept",
)
def accept_invitation(request: HttpRequest, payload: InvitationAcceptIn) -> Status[None]:
    invitations.accept(token=payload.token, password=payload.password, full_name=payload.full_name)
    return Status(204, None)
