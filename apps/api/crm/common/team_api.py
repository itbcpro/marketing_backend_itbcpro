"""The people of a workspace, for owner and assignee pickers across the CRM."""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router, Schema

from identity.authentication import api_auth
from workspaces import services as workspaces
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-team"], auth=api_auth)


class MemberOut(Schema):
    user_id: uuid.UUID
    name: str
    email: str
    role: str


@router.get("/team", response=list[MemberOut], operation_id="crm_team_list")
def list_team(request: HttpRequest) -> list[MemberOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return [
        MemberOut(
            user_id=m.user_id,
            name=m.user.full_name or m.user.email,
            email=m.user.email,
            role=str(m.role),
        )
        for m in workspaces.list_members(membership.workspace_id)
    ]
