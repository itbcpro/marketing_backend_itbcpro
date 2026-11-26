"""Inviting people to a workspace and accepting invitations."""

import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import (
    ConflictError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from identity.models import User
from notifications.email import send_templated_email
from workspaces.models import Invitation, Membership, Role, Workspace
from workspaces.permissions import Permission
from workspaces.services import require_permission


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def invite(*, workspace: Workspace, email: str, role: str, invited_by: User) -> Invitation:
    require_permission(invited_by, workspace.pk, Permission.MANAGE_MEMBERS)
    if role == Role.OWNER:
        raise PermissionDeniedError("owners cannot be invited")
    address = email.strip().lower()
    if Membership.objects.filter(workspace=workspace, user__email=address).exists():
        raise ConflictError("already a member")
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        Invitation.objects.filter(
            workspace=workspace, email=address, accepted_at__isnull=True, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        invitation = Invitation.objects.create(
            workspace=workspace,
            email=address,
            role=role,
            token_hash=_hash(token),
            invited_by=invited_by,
            expires_at=timezone.now() + timedelta(days=settings.INVITATION_TTL_DAYS),
        )
        record_event(
            action="invitation.created",
            actor=Actor.human(invited_by.pk),
            workspace_id=workspace.pk,
            target_type="invitation",
            target_id=invitation.pk,
            after={"email": address, "role": role},
        )
    send_templated_email(
        address,
        "invitation",
        {
            "workspace": workspace.name,
            "inviter": invited_by.full_name or invited_by.email,
            "role": Role(role).label,
            "days": settings.INVITATION_TTL_DAYS,
            "link": f"{settings.WEB_BASE_URL}/accept-invitation?token={token}",
        },
    )
    return invitation


def _usable(token: str) -> Invitation:
    invitation = (
        Invitation.objects.select_related("workspace")
        .filter(token_hash=_hash(token), accepted_at__isnull=True, revoked_at__isnull=True)
        .first()
    )
    if invitation is None or invitation.expires_at <= timezone.now():
        raise ValidationFailedError(code=ErrorCode.INVALID_TOKEN)
    return invitation


def accept(*, token: str, password: str | None = None, full_name: str = "") -> User:
    """Accept an invitation. Possession of the emailed token proves ownership of the address."""
    invitation = _usable(token)
    with transaction.atomic():
        user = User.objects.filter(email=invitation.email).first()
        if user is None:
            if not password:
                raise ValidationFailedError(code=ErrorCode.WEAK_PASSWORD)
            candidate = User(email=invitation.email, full_name=full_name[:200])
            try:
                validate_password(password, candidate)
            except DjangoValidationError as exc:
                raise ValidationFailedError(
                    "; ".join(exc.messages), code=ErrorCode.WEAK_PASSWORD
                ) from exc
            user = User.objects.create_user(invitation.email, password, full_name=full_name[:200])
        Membership.objects.update_or_create(
            workspace=invitation.workspace, user=user, defaults={"role": invitation.role}
        )
        invitation.accepted_at = timezone.now()
        invitation.save(update_fields=["accepted_at", "updated_at"])
        record_event(
            action="invitation.accepted",
            actor=Actor.human(user.pk),
            workspace_id=invitation.workspace_id,
            target_type="invitation",
            target_id=invitation.pk,
        )
    return user


def revoke(*, workspace: Workspace, invitation_id: uuid.UUID, revoked_by: User) -> None:
    require_permission(revoked_by, workspace.pk, Permission.MANAGE_MEMBERS)
    updated = Invitation.objects.filter(
        pk=invitation_id, workspace=workspace, accepted_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())
    if not updated:
        raise NotFoundError("invitation not found")
    record_event(
        action="invitation.revoked",
        actor=Actor.human(revoked_by.pk),
        workspace_id=workspace.pk,
        target_type="invitation",
        target_id=invitation_id,
    )


def list_pending(*, workspace: Workspace, viewer: User) -> list[Invitation]:
    require_permission(viewer, workspace.pk, Permission.MANAGE_MEMBERS)
    return list(
        Invitation.objects.filter(
            workspace=workspace,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).order_by("-created_at")
    )
