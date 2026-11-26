"""Record scope: restrict a queryset to what a member may see (lists, search, exports)."""

from django.db.models import Model, QuerySet

from workspaces.models import Membership, RecordScope


def restrict[T: Model](
    queryset: QuerySet[T], membership: Membership, *, owner_field: str = "assigned_to_id"
) -> QuerySet[T]:
    """Return only records assigned to the member when their scope is ``assigned``."""
    if membership.record_scope == RecordScope.ASSIGNED:
        return queryset.filter(**{owner_field: membership.user_id})
    return queryset


def can_see(membership: Membership, owner_id: object) -> bool:
    """Single-record variant for detail endpoints."""
    return membership.record_scope == RecordScope.ALL or owner_id == membership.user_id
