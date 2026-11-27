"""Read side of contacts: scoped queries, search and batch serialisation."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from django.db.models import F, Q, QuerySet
from django.db.models.functions import Lower

from core.api.filtering import FilterSpec, apply_filters
from core.errors import ValidationFailedError
from crm.contacts import tags as tag_service
from crm.contacts.filters import compile_filter
from crm.contacts.models import Contact, ContactFollower, ContactIdentity, IdentityChannel
from crm.fields import services as fields
from workspaces.models import Membership
from workspaces.scope import restrict

FILTERS = {
    "assigned_to": FilterSpec("assigned_to_id", uuid.UUID),
    "contact_type": FilterSpec("contact_type"),
    "source": FilterSpec("source"),
    "company_id": FilterSpec("company_id", uuid.UUID),
    "created_after": FilterSpec("created_at__gte", datetime.fromisoformat),
    "created_before": FilterSpec("created_at__lt", datetime.fromisoformat),
    "tag": FilterSpec("tag_links__tag__name__iexact"),
}
MAX_SEARCH_TERMS = 5


def visible_contacts(membership: Membership, *, deleted: bool = False) -> QuerySet[Contact]:
    """Contacts the member may see: workspace scoped, record scope applied."""
    rows = Contact.objects.for_workspace(membership.workspace_id)
    rows = rows.filter(deleted_at__isnull=not deleted)
    if deleted:
        rows = rows.filter(merged_into__isnull=True)
    return restrict(rows, membership)


def search(rows: QuerySet[Contact], text: str | None) -> QuerySet[Contact]:
    """Every term must match the name, email, phone or company name."""
    for term in (text or "").split()[:MAX_SEARCH_TERMS]:
        rows = rows.filter(
            Q(first_name__icontains=term)
            | Q(last_name__icontains=term)
            | Q(email__icontains=term)
            | Q(phone__icontains=term)
            | Q(company__name__icontains=term)
        )
    return rows


def filtered(
    membership: Membership, params: dict[str, str], text: str | None, *, deleted: bool = False
) -> QuerySet[Contact]:
    rows = apply_filters(visible_contacts(membership, deleted=deleted), params, FILTERS)
    return search(rows, text).distinct()


def query(
    membership: Membership,
    *,
    spec: dict[str, Any] | None = None,
    text: str | None = None,
    params: dict[str, str] | None = None,
    deleted: bool = False,
) -> QuerySet[Contact]:
    """Visible contacts narrowed by a filter spec, plain filters and a text search."""
    rows = apply_filters(visible_contacts(membership, deleted=deleted), params or {}, FILTERS)
    rows = rows.filter(compile_filter(membership.workspace_id, spec))
    return search(rows, text).distinct()


SORTS = {
    "newest": ("-created_at", "-pk"),
    "oldest": ("created_at", "pk"),
    "name": (Lower("first_name"), Lower("last_name"), "pk"),
    "last_activity": (F("last_activity_at").desc(nulls_last=True), "pk"),
}
DEFAULT_SORT = "newest"


def sorted_page(
    rows: QuerySet[Contact], *, sort: str, offset: int, limit: int
) -> tuple[list[Contact], int | None]:
    """One page in the chosen order, and the offset of the next page (None at the end).

    Ordering by something other than creation cannot use the id cursor, so this pages by offset.
    """
    if sort not in SORTS:
        raise ValidationFailedError("invalid sort", errors={"sort": "invalid_value"})
    ordered = rows.order_by(*SORTS[sort])
    found = list(ordered[offset : offset + limit + 1])
    more = len(found) > limit
    return found[:limit], offset + limit if more else None


def get_visible(membership: Membership, contact_id: uuid.UUID) -> Contact | None:
    return visible_contacts(membership).select_related("company").filter(pk=contact_id).first()


def serialise(workspace_id: uuid.UUID, contacts: list[Contact]) -> list[dict[str, Any]]:
    """Full representation of many contacts with a fixed number of queries."""
    ids = [c.pk for c in contacts]
    extras: dict[uuid.UUID, dict[str, list[str]]] = defaultdict(
        lambda: {"extra_emails": [], "extra_phones": []}
    )
    for row in ContactIdentity.objects.filter(
        contact_id__in=ids,
        is_active=True,
        is_primary=False,
        channel__in=[IdentityChannel.EMAIL, IdentityChannel.PHONE],
    ).order_by("created_at", "pk"):
        key = "extra_emails" if row.channel == IdentityChannel.EMAIL else "extra_phones"
        extras[row.contact_id][key].append(row.value)
    follower_ids: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for contact_id, user_id in ContactFollower.objects.filter(contact_id__in=ids).values_list(
        "contact_id", "user_id"
    ):
        follower_ids[contact_id].append(user_id)
    names = tag_service.names_of(ids)
    custom = fields.get_values(workspace_id, "contact", ids)
    return [
        {
            "id": c.pk,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "full_name": c.full_name,
            "email": c.email,
            "phone": c.phone,
            "extra_emails": extras[c.pk]["extra_emails"],
            "extra_phones": extras[c.pk]["extra_phones"],
            "company_id": c.company_id,
            "assigned_to": c.assigned_to_id,
            "followers": follower_ids.get(c.pk, []),
            "contact_type": c.contact_type,
            "source": c.source,
            "time_zone": c.time_zone,
            "date_of_birth": c.date_of_birth,
            "address": c.address,
            "city": c.city,
            "state": c.state,
            "postal_code": c.postal_code,
            "country": c.country,
            "tags": names.get(c.pk, []),
            "custom": custom.get(c.pk, {}),
            "attribution": c.attribution,
            "last_activity_at": c.last_activity_at,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
            "deleted_at": c.deleted_at,
        }
        for c in contacts
    ]
