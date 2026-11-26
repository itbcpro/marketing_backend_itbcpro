"""Which company a request is about: the workspace check plus the chosen brand."""

import uuid
from dataclasses import dataclass

from django.http import HttpRequest

from studio import brands
from studio.models import Brand
from workspaces.deps import resolve_workspace
from workspaces.models import Membership
from workspaces.permissions import Permission

BRAND_HEADER = "X-Studio-Brand"


@dataclass(frozen=True, slots=True)
class Scope:
    membership: Membership
    brand: Brand

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.membership.workspace_id


def brand_scope(request: HttpRequest, permission: Permission = Permission.VIEW) -> Scope:
    """The caller's membership and the company named by the ``X-Studio-Brand`` header (or the
    only company there is)."""
    membership = resolve_workspace(request, permission)
    brand = brands.resolve(membership.workspace_id, request.headers.get(BRAND_HEADER, ""))
    return Scope(membership, brand)
