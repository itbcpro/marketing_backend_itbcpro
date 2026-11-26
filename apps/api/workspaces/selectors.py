"""Read-side queries for workspaces."""

import uuid

from workspaces.models import Membership, Workspace


def workspaces_for_user(user_id: uuid.UUID) -> list[Membership]:
    return list(
        Membership.objects.select_related("workspace")
        .filter(user_id=user_id, workspace__is_active=True)
        .order_by("workspace__name")
    )


def workspace_by_slug(slug: str) -> Workspace | None:
    return Workspace.objects.filter(slug=slug).first()
