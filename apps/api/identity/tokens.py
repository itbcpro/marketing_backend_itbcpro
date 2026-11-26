"""Machine API tokens: ``itb_<prefix>_<secret>``. Only a hash of the secret is stored."""

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import NotFoundError, ValidationFailedError
from identity.models import ApiToken, User

TOKEN_PREFIX = "itb"  # noqa: S105 - public identifier, not a secret
ALLOWED_SCOPES = frozenset({"read", "write"})
LAST_USED_GRANULARITY = timedelta(minutes=5)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_token(
    user: User, *, name: str, scopes: list[str], expires_in_days: int | None = None
) -> tuple[ApiToken, str]:
    """Create a token and return it with the plaintext value, which is shown only once."""
    if not scopes or not set(scopes) <= ALLOWED_SCOPES:
        raise ValidationFailedError("invalid scopes")
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    with transaction.atomic():
        token = ApiToken.objects.create(
            user=user,
            name=name,
            prefix=prefix,
            token_hash=_hash(secret),
            scopes=sorted(set(scopes)),
            expires_at=timezone.now() + timedelta(days=expires_in_days)
            if expires_in_days
            else None,
        )
        record_event(
            action="api_token.created",
            actor=Actor.human(user.pk),
            target_type="api_token",
            target_id=token.pk,
            after={"name": name, "scopes": token.scopes},
        )
    return token, f"{TOKEN_PREFIX}_{prefix}_{secret}"


def revoke_token(user: User, token_id: uuid.UUID) -> None:
    updated = ApiToken.objects.filter(pk=token_id, user=user, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    if not updated:
        raise NotFoundError("token not found")
    record_event(
        action="api_token.revoked",
        actor=Actor.human(user.pk),
        target_type="api_token",
        target_id=token_id,
    )


def authenticate(raw: str, *, method: str) -> ApiToken | None:
    """Return the token for a valid bearer value, enforcing expiry, revocation and scope."""
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:  # noqa: PLR2004 - itb_<prefix>_<secret>
        return None
    token = ApiToken.objects.select_related("user").filter(prefix=parts[1]).first()
    now = timezone.now()
    if (
        token is None
        or token.revoked_at is not None
        or (token.expires_at is not None and token.expires_at <= now)
        or not token.user.is_active
        or not hmac.compare_digest(token.token_hash, _hash(parts[2]))
    ):
        return None
    safe = method in {"GET", "HEAD", "OPTIONS"}
    if not safe and "write" not in token.scopes:
        return None
    if token.last_used_at is None or now - token.last_used_at > LAST_USED_GRANULARITY:
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return token
