"""Workspace endpoints."""

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from core.errors import NotFoundError
from identity.authentication import api_auth
from identity.models import User
from workspaces import invitations
from workspaces.deps import resolve_workspace
from workspaces.models import Invitation, Workspace
from workspaces.permissions import Permission
from workspaces.selectors import workspaces_for_user

router = Router(tags=["workspaces"], auth=api_auth)


class WorkspaceOut(Schema):
    id: str
    name: str
    slug: str
    role: str
    default_locale: str
    reporting_currency: str
    writes_paused: bool


class InvitationIn(Schema):
    email: str
    role: str = "viewer"


class InvitationOut(Schema):
    id: uuid.UUID
    email: str
    role: str
    expires_at: datetime


@router.get("/", response=list[WorkspaceOut], operation_id="workspaces_list")
def list_workspaces(request: HttpRequest) -> list[WorkspaceOut]:
    user: User = request.auth  # type: ignore[attr-defined]
    return [
        WorkspaceOut(
            id=str(m.workspace_id),
            name=m.workspace.name,
            slug=m.workspace.slug,
            role=m.role,
            default_locale=m.workspace.default_locale,
            reporting_currency=m.workspace.reporting_currency,
            writes_paused=m.workspace.writes_paused,
        )
        for m in workspaces_for_user(user.pk)
    ]


def _workspace(request: HttpRequest) -> tuple[Workspace, User]:
    membership = resolve_workspace(request, Permission.MANAGE_MEMBERS)
    return membership.workspace, membership.user


@router.get("/invitations", response=list[InvitationOut], operation_id="invitations_list")
def list_invitations(request: HttpRequest) -> list[Invitation]:
    workspace, user = _workspace(request)
    return invitations.list_pending(workspace=workspace, viewer=user)


@router.post("/invitations", response=InvitationOut, operation_id="invitations_create")
def create_invitation(request: HttpRequest, payload: InvitationIn) -> Invitation:
    workspace, user = _workspace(request)
    return invitations.invite(
        workspace=workspace, email=payload.email, role=payload.role, invited_by=user
    )


@router.delete(
    "/invitations/{invitation_id}", response={204: None}, operation_id="invitations_revoke"
)
def revoke_invitation(request: HttpRequest, invitation_id: uuid.UUID) -> Status[None]:
    workspace, user = _workspace(request)
    try:
        invitations.revoke(workspace=workspace, invitation_id=invitation_id, revoked_by=user)
    except NotFoundError:
        raise
    return Status(204, None)
