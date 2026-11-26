"""Request-scoped workspace resolution and authorisation for API endpoints."""

import uuid

from django.http import HttpRequest

from core import tenancy
from core.errors import ErrorCode, NotFoundError, PermissionDeniedError, ValidationFailedError
from identity.models import User
from workspaces.models import Membership
from workspaces.permissions import Permission
from workspaces.services import require_permission

WORKSPACE_HEADER = "X-Workspace-Id"


def resolve_workspace(request: HttpRequest, permission: Permission = Permission.VIEW) -> Membership:
    """Return the caller's membership for the requested workspace after checking permission.

    Unknown workspaces and workspaces the caller does not belong to are indistinguishable
    (both surface as not found) so ids cannot be enumerated.
    """
    return _membership(
        request,
        _requested_workspace(request, request.headers.get(WORKSPACE_HEADER, "")),
        permission,
    )


def resolve_workspace_for_file(
    request: HttpRequest, permission: Permission = Permission.VIEW
) -> Membership:
    """Like ``resolve_workspace``, for downloads shown by plain ``<img>`` and ``<video>`` tags.

    Those tags cannot send a header, so the workspace may also come from the ``workspace`` query
    parameter. Membership and permission are checked exactly the same way; the id is not a secret.
    """
    raw = request.headers.get(WORKSPACE_HEADER) or request.GET.get("workspace", "")
    return _membership(request, _requested_workspace(request, raw), permission)


def _requested_workspace(request: HttpRequest, raw: str) -> str:
    """A JWT carries its workspace, so the header may be omitted when one is used."""
    claims = getattr(request, "jwt_claims", None)
    return raw or (str(claims.workspace_id) if claims is not None else "")


def _membership(request: HttpRequest, raw: str, permission: Permission) -> Membership:
    try:
        workspace_id = uuid.UUID(raw)
    except ValueError as exc:
        raise ValidationFailedError(code=ErrorCode.WORKSPACE_REQUIRED) from exc
    user = request.user
    if not isinstance(user, User):
        raise PermissionDeniedError()
    claims = getattr(request, "jwt_claims", None)
    if claims is not None and workspace_id != claims.workspace_id:
        # A token is valid for exactly one workspace; any other id looks like an unknown one.
        raise ValidationFailedError(code=ErrorCode.WORKSPACE_REQUIRED)
    try:
        membership = require_permission(user, workspace_id, permission)
    except NotFoundError as exc:
        raise ValidationFailedError(code=ErrorCode.WORKSPACE_REQUIRED) from exc
    tenancy.activate(membership.workspace_id)
    return membership
