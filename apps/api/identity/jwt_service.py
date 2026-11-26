"""JWT access tokens and single-use refresh tokens (ADR 0008)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ErrorCode, UnauthenticatedError
from core.ids import new_id
from core.logging import get_logger
from identity.jwt_keys import get_keyring
from identity.models import RefreshToken, User
from workspaces.models import Membership
from workspaces.permissions import ROLE_PERMISSIONS

log = get_logger(__name__)
REVOKED_KEY = "jwt:revoked-sid:{sid}"
TOKEN_TYPE = "Bearer"  # noqa: S105 - OAuth token type label, not a secret


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    workspace_id: uuid.UUID
    role: str


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    role: str
    scopes: tuple[str, ...]
    session_id: uuid.UUID
    token_id: str


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _scopes_for(role: str) -> list[str]:
    return sorted(p.value for p in ROLE_PERMISSIONS.get(role, frozenset()))


def _mint_access(user: User, membership: Membership, family_id: uuid.UUID) -> tuple[str, int]:
    now = timezone.now()
    ttl = settings.JWT_ACCESS_TTL_SECONDS
    claims: dict[str, Any] = {
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "sub": str(user.pk),
        "ws": str(membership.workspace_id),
        "role": membership.role,
        "scp": _scopes_for(membership.role),
        "sid": str(family_id),
        "jti": str(new_id()),
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=ttl),
    }
    return get_keyring().sign(claims), ttl


def _new_refresh(
    user: User,
    membership: Membership,
    family_id: uuid.UUID,
    *,
    expires_at: Any,
    user_agent: str,
    ip: str | None,
) -> str:
    raw = secrets.token_urlsafe(48)
    now = timezone.now()
    idle = now + timedelta(days=settings.JWT_REFRESH_IDLE_DAYS)
    RefreshToken.objects.create(
        user=user,
        workspace_id=membership.workspace_id,
        family_id=family_id,
        token_hash=_hash(raw),
        expires_at=expires_at,
        idle_expires_at=min(idle, expires_at),
        user_agent=user_agent[:300],
        ip_address=ip,
    )
    return raw


def _pair(
    user: User, membership: Membership, family_id: uuid.UUID, refresh: str, exp: Any
) -> TokenPair:
    access, ttl = _mint_access(user, membership, family_id)
    remaining = int((exp - timezone.now()).total_seconds())
    return TokenPair(
        access, ttl, refresh, max(remaining, 0), membership.workspace_id, membership.role
    )


def issue_pair(
    user: User, membership: Membership, *, user_agent: str = "", ip: str | None = None
) -> TokenPair:
    """Start a new login family for one workspace. The caller has already authenticated the user."""
    family_id = new_id()
    expires_at = timezone.now() + timedelta(days=settings.JWT_REFRESH_TTL_DAYS)
    with transaction.atomic():
        refresh = _new_refresh(
            user, membership, family_id, expires_at=expires_at, user_agent=user_agent, ip=ip
        )
        record_event(
            action="auth.token_issued",
            actor=Actor.human(user.pk),
            target_type="workspace",
            target_id=membership.workspace_id,
        )
    return _pair(user, membership, family_id, refresh, expires_at)


def _reject() -> UnauthenticatedError:
    return UnauthenticatedError(code=ErrorCode.INVALID_TOKEN)


def revoke_family(family_id: uuid.UUID) -> None:
    now = timezone.now()
    RefreshToken.objects.filter(family_id=family_id, revoked_at__isnull=True).update(revoked_at=now)
    ttl = settings.JWT_ACCESS_TTL_SECONDS + settings.JWT_LEEWAY_SECONDS
    cache.set(REVOKED_KEY.format(sid=family_id), 1, timeout=ttl)


def revoke_user(user: User) -> int:
    families = list(
        RefreshToken.objects.filter(user=user, revoked_at__isnull=True)
        .values_list("family_id", flat=True)
        .distinct()
    )
    for family_id in families:
        revoke_family(family_id)
    return len(families)


def rotate(raw: str, *, user_agent: str = "", ip: str | None = None) -> TokenPair:
    """Exchange a refresh token for a new pair. Presenting a used token revokes its family."""
    reused: RefreshToken | None = None
    with transaction.atomic():
        token = (
            RefreshToken.objects.select_for_update()
            .select_related("user")
            .filter(token_hash=_hash(raw))
            .first()
        )
        if token is None:
            raise _reject()
        now = timezone.now()
        if token.rotated_at is not None:
            reused = token  # handled after the transaction so the revocation is committed
        else:
            return _rotate_locked(token, now, user_agent=user_agent, ip=ip)
    if reused is not None:
        _handle_reuse(reused)
    raise _reject()


def _handle_reuse(token: RefreshToken) -> None:
    record_event(
        action="auth.refresh_reuse_detected",
        actor=Actor.system("auth"),
        target_type="user",
        target_id=token.user_id,
    )
    revoke_family(token.family_id)
    log.warning("refresh_reuse_detected", family=str(token.family_id))


def _rotate_locked(token: RefreshToken, now: Any, *, user_agent: str, ip: str | None) -> TokenPair:
    membership = Membership.objects.filter(
        user=token.user, workspace_id=token.workspace_id, workspace__is_active=True
    ).first()
    if (
        token.revoked_at is not None
        or token.expires_at <= now
        or token.idle_expires_at <= now
        or membership is None
        or not token.user.is_active
        or token.user.is_locked
    ):
        raise _reject()
    token.rotated_at = now
    token.save(update_fields=["rotated_at", "updated_at"])
    refresh = _new_refresh(
        token.user,
        membership,
        token.family_id,
        expires_at=token.expires_at,
        user_agent=user_agent,
        ip=ip,
    )
    return _pair(token.user, membership, token.family_id, refresh, token.expires_at)


def revoke_by_refresh(raw: str) -> None:
    token = RefreshToken.objects.filter(token_hash=_hash(raw)).first()
    if token is not None:
        revoke_family(token.family_id)


def verify_access(token: str) -> tuple[AccessClaims, User]:
    """Verify signature and claims, the revocation list and the user's current state."""
    claims = get_keyring().verify(token)
    try:
        session_id = uuid.UUID(claims["sid"])
        user_id = uuid.UUID(claims["sub"])
        workspace_id = uuid.UUID(claims["ws"])
    except ValueError as exc:
        raise _reject() from exc
    try:
        revoked = cache.get(REVOKED_KEY.format(sid=session_id)) is not None
    except Exception:
        log.exception("jwt_revocation_check_failed")
        revoked = True  # fail closed: an unreachable deny-list must not let revoked tokens pass
    if revoked:
        raise _reject()
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None or user.is_locked:
        raise _reject()
    return (
        AccessClaims(
            user_id=user_id,
            workspace_id=workspace_id,
            role=str(claims.get("role", "")),
            scopes=tuple(claims.get("scp", [])),
            session_id=session_id,
            token_id=str(claims["jti"]),
        ),
        user,
    )
