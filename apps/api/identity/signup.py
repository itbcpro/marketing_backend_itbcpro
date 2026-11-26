"""Self-service sign-up: an inactive account and its organization, activated by email.

Nothing about an existing address is revealed: whoever asks gets the same answer, and the
address's owner gets an email that says what happened.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.utils.text import slugify

from audit.actors import Actor
from audit.services import record_event
from core.errors import ErrorCode, NotFoundError, ValidationFailedError
from identity.models import User
from notifications.email import send_templated_email
from workspaces.models import Workspace
from workspaces.services import create_workspace

NAME_LIMIT = 100
EMAIL_LIMIT = 254
SLUG_LIMIT = 40
SUFFIX = 6


def _link(user: User, path: str) -> str:
    uid = urlsafe_base64_encode(force_bytes(str(user.pk)))
    token = default_token_generator.make_token(user)
    return f"{settings.WEB_BASE_URL}/{path}?uid={uid}&token={token}"


def _slug(name: str) -> str:
    base = slugify(name)[:SLUG_LIMIT] or "workspace"
    if not Workspace.objects.filter(slug=base).exists():
        return base
    return f"{base}-{uuid.uuid4().hex[:SUFFIX]}"


def _check(email: str, password: str, organization_name: str) -> None:
    errors: dict[str, str] = {}
    if "@" not in email or len(email) > EMAIL_LIMIT:
        errors["email"] = "invalid"
    if not organization_name.strip() or len(organization_name.strip()) > NAME_LIMIT:
        errors["organization_name"] = "invalid"
    if errors:
        raise ValidationFailedError("invalid sign-up", errors=errors)
    try:
        validate_password(password)
    except DjangoValidationError as exc:
        raise ValidationFailedError("; ".join(exc.messages), code=ErrorCode.WEAK_PASSWORD) from exc


def sign_up(*, email: str, password: str, full_name: str, organization_name: str) -> None:
    """Create the account and send the link. Callers answer the same whatever happens here."""
    if not settings.SIGNUP_ENABLED:
        raise NotFoundError("not found")
    address = email.strip().lower()
    _check(address, password, organization_name)
    existing = User.objects.filter(email=address).first()
    if existing is not None:
        if existing.is_active:
            send_templated_email(
                address, "account_exists", {"link": f"{settings.WEB_BASE_URL}/login"}
            )
        else:  # signed up before and never confirmed: send the link again
            send_templated_email(address, "verify_email", {"link": _link(existing, "verify-email")})
        return
    with transaction.atomic():
        user = User.objects.create_user(
            address, password, full_name=full_name.strip()[:200], is_active=False
        )
        name = " ".join(organization_name.split())
        workspace = create_workspace(name=name, owner=user, slug=_slug(name))
        record_event(
            action="auth.signed_up",
            actor=Actor.human(user.pk),
            workspace_id=workspace.pk,
            target_type="user",
            target_id=user.pk,
        )
    send_templated_email(address, "verify_email", {"link": _link(user, "verify-email")})


def verify_email(*, uid: str, token: str) -> User:
    try:
        user = User.objects.filter(pk=force_str(urlsafe_base64_decode(uid))).first()
    except ValueError, TypeError, OverflowError:
        user = None
    if user is None or not default_token_generator.check_token(user, token):
        raise ValidationFailedError(code=ErrorCode.INVALID_TOKEN)
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active", "updated_at"])
        record_event(
            action="auth.email_verified",
            actor=Actor.human(user.pk),
            target_type="user",
            target_id=user.pk,
        )
    return user
