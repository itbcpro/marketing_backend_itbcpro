"""Contacts, companies, tags, merge and CRM settings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Schema, Status

from audit.actors import Actor
from core.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, Page, paginate
from core.errors import PermissionDeniedError, ValidationFailedError
from crm.contacts import bulk, selectors, services
from crm.contacts.models import Company, MatchOrder
from crm.contacts.schemas import ContactOut
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.models import Membership
from workspaces.permissions import Permission

router = Router(tags=["crm-contacts"], auth=api_auth)


# ---- schemas ---------------------------------------------------------------------------------


class ContactIn(Schema):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    extra_emails: list[str] | None = None
    extra_phones: list[str] | None = None
    company_id: uuid.UUID | None = None
    assigned_to: uuid.UUID | None = None
    contact_type: str | None = None
    source: str | None = None
    time_zone: str | None = None
    date_of_birth: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    attribution: dict[str, Any] | None = None
    custom: dict[str, Any] | None = None
    tags: list[str] | None = None


class UpsertOut(Schema):
    created: bool
    contact: ContactOut


class ContactQuery(Schema):
    q: str | None = None
    assigned_to: uuid.UUID | None = None
    contact_type: str | None = None
    source: str | None = None
    company_id: uuid.UUID | None = None
    created_after: str | None = None
    created_before: str | None = None
    tag: str | None = None
    deleted: bool = False
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT


class ContactSearchIn(Schema):
    """A list view: any filter of the contact filter language, a text search and a sort."""

    filter: dict[str, Any] | None = None
    q: str | None = None
    sort: str = selectors.DEFAULT_SORT
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT


class ContactAssignIn(Schema):
    user_id: uuid.UUID | None


class ContactBulkIn(Schema):
    ids: list[uuid.UUID]
    action: str
    user_id: uuid.UUID | None = None
    names: list[str] = []


class ContactBulkOut(Schema):
    done: int
    skipped: int


class TagNamesIn(Schema):
    names: list[str]


class MergeIn(Schema):
    winner_id: uuid.UUID
    loser_id: uuid.UUID
    choices: dict[str, str] = {}


class MergeOut(Schema):
    id: uuid.UUID
    winner_id: uuid.UUID
    loser_id: uuid.UUID
    created_at: datetime
    undone_at: datetime | None


class TagOut(Schema):
    id: uuid.UUID
    name: str
    color: str


class TagRenameIn(Schema):
    name: str


class TagMergeIn(Schema):
    target_id: uuid.UUID


class CompanyIn(Schema):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    description: str | None = None
    assigned_to: uuid.UUID | None = None
    custom: dict[str, Any] | None = None


class CompanyOut(Schema):
    id: uuid.UUID
    name: str
    email: str
    phone: str
    website: str
    address: str
    description: str
    assigned_to_id: uuid.UUID | None
    created_at: datetime


class ContactSettingsOut(Schema):
    match_order: str


class ContactSettingsIn(Schema):
    match_order: str


# ---- helpers ---------------------------------------------------------------------------------


def _actor(membership: Membership) -> Actor:
    return Actor.human(membership.user_id)


def _visible_or_404(membership: Membership, contact_id: uuid.UUID) -> None:
    services.get_visible_contact(membership, contact_id)


def _one(membership: Membership, contact_id: uuid.UUID) -> dict[str, Any]:
    contact = services.get_contact(membership.workspace_id, contact_id, include_deleted=True)
    return selectors.serialise(membership.workspace_id, [contact])[0]


def _payload(payload: Schema) -> dict[str, Any]:
    return payload.model_dump(exclude_unset=True)


# ---- contacts --------------------------------------------------------------------------------


@router.get("/contacts", response=Page[ContactOut], operation_id="crm_contacts_list")
def list_contacts(request: HttpRequest, query: Query[ContactQuery]) -> Page[ContactOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    params = {k: str(v) for k in selectors.FILTERS if (v := getattr(query, k)) is not None}
    rows = selectors.filtered(membership, params, query.q, deleted=query.deleted)
    page, next_cursor = paginate(rows, cursor=query.cursor, limit=query.limit)
    items = selectors.serialise(membership.workspace_id, page)
    return Page[ContactOut](items=[ContactOut(**i) for i in items], next_cursor=next_cursor)


@router.post("/contacts/search", response=Page[ContactOut], operation_id="crm_contacts_search")
def search_contacts(request: HttpRequest, payload: ContactSearchIn) -> Page[ContactOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    try:
        offset = int(payload.cursor or 0)
    except ValueError as exc:
        raise ValidationFailedError("invalid cursor", errors={"cursor": "invalid_value"}) from exc
    rows = selectors.query(membership, spec=payload.filter, text=payload.q)
    limit = max(1, min(payload.limit, MAX_LIMIT))
    page, following = selectors.sorted_page(rows, sort=payload.sort, offset=offset, limit=limit)
    items = selectors.serialise(membership.workspace_id, page)
    next_cursor = None if following is None else str(following)
    return Page[ContactOut](items=[ContactOut(**i) for i in items], next_cursor=next_cursor)


@router.post("/contacts", response={201: ContactOut}, operation_id="crm_contacts_create")
def create_contact(request: HttpRequest, payload: ContactIn) -> Status[ContactOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    contact = services.create_contact(
        workspace_id=membership.workspace_id, actor=_actor(membership), data=_payload(payload)
    )
    return Status(201, ContactOut(**_one(membership, contact.pk)))


@router.post("/contacts/upsert", response=UpsertOut, operation_id="crm_contacts_upsert")
def upsert_contact(request: HttpRequest, payload: ContactIn) -> UpsertOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    contact, created = services.upsert_contact(
        workspace_id=membership.workspace_id, actor=_actor(membership), data=_payload(payload)
    )
    return UpsertOut(created=created, contact=ContactOut(**_one(membership, contact.pk)))


@router.post("/contacts/merge", response={201: MergeOut}, operation_id="crm_contacts_merge")
def merge_contacts(request: HttpRequest, payload: MergeIn) -> Status[MergeOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_or_404(membership, payload.winner_id)
    _visible_or_404(membership, payload.loser_id)
    merge = services.merge_contacts(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        winner_id=payload.winner_id,
        loser_id=payload.loser_id,
        choices=payload.choices,
    )
    return Status(
        201,
        MergeOut(
            id=merge.pk,
            winner_id=merge.winner_id,
            loser_id=merge.loser_id,
            created_at=merge.created_at,
            undone_at=merge.undone_at,
        ),
    )


@router.post(
    "/contacts/merges/{merge_id}/undo", response=MergeOut, operation_id="crm_contacts_merge_undo"
)
def undo_merge(request: HttpRequest, merge_id: uuid.UUID) -> MergeOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    merge = services.undo_merge(
        workspace_id=membership.workspace_id, actor=_actor(membership), merge_id=merge_id
    )
    return MergeOut(
        id=merge.pk,
        winner_id=merge.winner_id,
        loser_id=merge.loser_id,
        created_at=merge.created_at,
        undone_at=merge.undone_at,
    )


@router.post("/contacts/bulk", response=ContactBulkOut, operation_id="crm_contacts_bulk")
def bulk_action(request: HttpRequest, payload: ContactBulkIn) -> ContactBulkOut:
    """One action on many contacts; contacts the member cannot see are skipped, never revealed."""
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    if payload.action in bulk.ACTIONS and not bulk.allowed(membership, payload.action):
        raise PermissionDeniedError("not allowed")
    ids = list(dict.fromkeys(payload.ids))
    visible = list(
        selectors.visible_contacts(membership).filter(pk__in=ids).values_list("pk", flat=True)
    )
    result = bulk.run(
        membership,
        _actor(membership),
        action=payload.action,
        visible_ids=visible,
        requested=len(ids),
        user_id=payload.user_id,
        names=payload.names,
    )
    return ContactBulkOut(**result)


@router.get("/contacts/{contact_id}", response=ContactOut, operation_id="crm_contacts_get")
def get_contact(request: HttpRequest, contact_id: uuid.UUID) -> ContactOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    _visible_or_404(membership, contact_id)
    return ContactOut(**_one(membership, contact_id))


@router.patch("/contacts/{contact_id}", response=ContactOut, operation_id="crm_contacts_update")
def update_contact(request: HttpRequest, contact_id: uuid.UUID, payload: ContactIn) -> ContactOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_or_404(membership, contact_id)
    services.update_contact(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        contact_id=contact_id,
        data=_payload(payload),
    )
    return ContactOut(**_one(membership, contact_id))


@router.delete("/contacts/{contact_id}", response={204: None}, operation_id="crm_contacts_delete")
def delete_contact(request: HttpRequest, contact_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_DELETE)
    _visible_or_404(membership, contact_id)
    services.delete_contact(
        workspace_id=membership.workspace_id, actor=_actor(membership), contact_id=contact_id
    )
    return Status(204, None)


@router.post(
    "/contacts/{contact_id}/restore", response=ContactOut, operation_id="crm_contacts_restore"
)
def restore_contact(request: HttpRequest, contact_id: uuid.UUID) -> ContactOut:
    membership = resolve_workspace(request, Permission.CRM_DELETE)
    services.restore_contact(
        workspace_id=membership.workspace_id, actor=_actor(membership), contact_id=contact_id
    )
    return ContactOut(**_one(membership, contact_id))


@router.post(
    "/contacts/{contact_id}/erase", response={204: None}, operation_id="crm_contacts_erase"
)
def erase_contact(request: HttpRequest, contact_id: uuid.UUID) -> Status[None]:
    """Privacy erasure; admins only because it cannot be undone."""
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    services.erase_contact(
        workspace_id=membership.workspace_id, actor=_actor(membership), contact_id=contact_id
    )
    return Status(204, None)


@router.post(
    "/contacts/{contact_id}/assign", response=ContactOut, operation_id="crm_contacts_assign"
)
def assign_contact(
    request: HttpRequest, contact_id: uuid.UUID, payload: ContactAssignIn
) -> ContactOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_or_404(membership, contact_id)
    services.assign(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        contact_id=contact_id,
        user_id=payload.user_id,
    )
    return ContactOut(**_one(membership, contact_id))


@router.post(
    "/contacts/{contact_id}/followers/{user_id}",
    response={204: None},
    operation_id="crm_contacts_follow",
)
def add_follower(request: HttpRequest, contact_id: uuid.UUID, user_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_or_404(membership, contact_id)
    services.add_follower(
        workspace_id=membership.workspace_id, contact_id=contact_id, user_id=user_id
    )
    return Status(204, None)


@router.delete(
    "/contacts/{contact_id}/followers/{user_id}",
    response={204: None},
    operation_id="crm_contacts_unfollow",
)
def remove_follower(
    request: HttpRequest, contact_id: uuid.UUID, user_id: uuid.UUID
) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_or_404(membership, contact_id)
    services.remove_follower(
        workspace_id=membership.workspace_id, contact_id=contact_id, user_id=user_id
    )
    return Status(204, None)


@router.post(
    "/contacts/{contact_id}/tags", response=ContactOut, operation_id="crm_contacts_tags_add"
)
def add_tags(request: HttpRequest, contact_id: uuid.UUID, payload: TagNamesIn) -> ContactOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_or_404(membership, contact_id)
    contact = services.get_contact(membership.workspace_id, contact_id)
    services.add_tags(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        contact=contact,
        names=payload.names,
    )
    return ContactOut(**_one(membership, contact_id))


@router.post(
    "/contacts/{contact_id}/tags/remove",
    response=ContactOut,
    operation_id="crm_contacts_tags_remove",
)
def remove_tags(request: HttpRequest, contact_id: uuid.UUID, payload: TagNamesIn) -> ContactOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_or_404(membership, contact_id)
    contact = services.get_contact(membership.workspace_id, contact_id)
    services.remove_tags(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        contact=contact,
        names=payload.names,
    )
    return ContactOut(**_one(membership, contact_id))


# ---- tags ------------------------------------------------------------------------------------


@router.get("/tags", response=list[TagOut], operation_id="crm_tags_list")
def list_tags(request: HttpRequest) -> list[TagOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return [
        TagOut(id=t.pk, name=t.name, color=t.color)
        for t in services.list_tags(membership.workspace_id)
    ]


@router.patch("/tags/{tag_id}", response=TagOut, operation_id="crm_tags_rename")
def rename_tag(request: HttpRequest, tag_id: uuid.UUID, payload: TagRenameIn) -> TagOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    tag = services.rename_tag(
        workspace_id=membership.workspace_id, tag_id=tag_id, name=payload.name
    )
    return TagOut(id=tag.pk, name=tag.name, color=tag.color)


@router.post("/tags/{tag_id}/merge", response=TagOut, operation_id="crm_tags_merge")
def merge_tag(request: HttpRequest, tag_id: uuid.UUID, payload: TagMergeIn) -> TagOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    tag = services.merge_tags(
        workspace_id=membership.workspace_id, source_id=tag_id, target_id=payload.target_id
    )
    return TagOut(id=tag.pk, name=tag.name, color=tag.color)


# ---- companies -------------------------------------------------------------------------------


def _company_out(company: Company) -> CompanyOut:
    return CompanyOut.model_validate(company, from_attributes=True)


@router.get("/companies", response=Page[CompanyOut], operation_id="crm_companies_list")
def list_companies(
    request: HttpRequest,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[CompanyOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = Company.objects.for_workspace(membership.workspace_id).filter(deleted_at__isnull=True)
    if q:
        rows = rows.filter(name__icontains=q)
    page, next_cursor = paginate(rows, cursor=cursor, limit=limit)
    return Page[CompanyOut](items=[_company_out(c) for c in page], next_cursor=next_cursor)


@router.post("/companies", response={201: CompanyOut}, operation_id="crm_companies_create")
def create_company(request: HttpRequest, payload: CompanyIn) -> Status[CompanyOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    company = services.create_company(
        workspace_id=membership.workspace_id, actor=_actor(membership), data=_payload(payload)
    )
    return Status(201, _company_out(company))


@router.get("/companies/{company_id}", response=CompanyOut, operation_id="crm_companies_get")
def get_company(request: HttpRequest, company_id: uuid.UUID) -> CompanyOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return _company_out(services.get_company(membership.workspace_id, company_id))


@router.patch("/companies/{company_id}", response=CompanyOut, operation_id="crm_companies_update")
def update_company(request: HttpRequest, company_id: uuid.UUID, payload: CompanyIn) -> CompanyOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    company = services.update_company(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        company_id=company_id,
        data=_payload(payload),
    )
    return _company_out(company)


@router.delete("/companies/{company_id}", response={204: None}, operation_id="crm_companies_delete")
def delete_company(request: HttpRequest, company_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_DELETE)
    services.delete_company(
        workspace_id=membership.workspace_id, actor=_actor(membership), company_id=company_id
    )
    return Status(204, None)


# ---- settings --------------------------------------------------------------------------------


@router.get("/settings", response=ContactSettingsOut, operation_id="crm_settings_get")
def get_settings(request: HttpRequest) -> ContactSettingsOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return ContactSettingsOut(
        match_order=services.get_settings(membership.workspace_id).match_order
    )


@router.put("/settings", response=ContactSettingsOut, operation_id="crm_settings_put")
def put_settings(request: HttpRequest, payload: ContactSettingsIn) -> ContactSettingsOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    if payload.match_order not in MatchOrder.values:
        raise ValidationFailedError("invalid", errors={"match_order": "invalid_option"})
    row = services.get_settings(membership.workspace_id)
    row.match_order = payload.match_order
    row.save(update_fields=["match_order", "updated_at"])
    return ContactSettingsOut(match_order=row.match_order)
