"""Smart lists, CSV import and export, bulk actions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from ninja import File, Query, Router, Schema, Status
from ninja.files import UploadedFile

from audit.actors import Actor
from core.api.pagination import DEFAULT_LIMIT, Page, paginate
from core.errors import ValidationFailedError
from crm.contacts import services as contacts
from crm.contacts.schemas import ContactOut
from crm.lists import bulk, csvio, export, imports, smart_lists
from crm.lists.models import BulkAction, BulkJob, ImportJob, SmartList
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace, resolve_workspace_for_file
from workspaces.models import Membership
from workspaces.permissions import Permission

router = Router(tags=["crm-lists"], auth=api_auth)

BULK_PERMISSION = {
    BulkAction.ADD_TAGS: Permission.CRM_EDIT,
    BulkAction.REMOVE_TAGS: Permission.CRM_EDIT,
    BulkAction.ASSIGN: Permission.CRM_EDIT,
    BulkAction.DELETE: Permission.CRM_DELETE,
}


def _actor(m: Membership) -> Actor:
    return Actor.human(m.user_id)


# ---- smart lists ---------------------------------------------------------------------------


class SmartListIn(Schema):
    name: str
    filter: dict[str, Any] = {}
    columns: list[str] = []
    visibility: str = "private"


class SmartListPatch(Schema):
    name: str | None = None
    filter: dict[str, Any] | None = None
    columns: list[str] | None = None
    visibility: str | None = None


class SmartListOut(Schema):
    id: uuid.UUID
    name: str
    filter: dict[str, Any]
    columns: list[str]
    visibility: str
    owner_id: uuid.UUID | None


def _list_out(row: SmartList) -> SmartListOut:
    return SmartListOut.model_validate(row, from_attributes=True)


@router.get("/smart-lists", response=list[SmartListOut], operation_id="crm_lists_list")
def list_smart_lists(request: HttpRequest) -> list[SmartListOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return [_list_out(r) for r in smart_lists.visible_lists(membership)]


@router.post("/smart-lists", response={201: SmartListOut}, operation_id="crm_lists_create")
def create_smart_list(request: HttpRequest, payload: SmartListIn) -> Status[SmartListOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    row = smart_lists.create(
        membership=membership,
        actor=_actor(membership),
        name=payload.name,
        spec=payload.filter,
        columns=payload.columns,
        visibility=payload.visibility,
    )
    return Status(201, _list_out(row))


@router.patch("/smart-lists/{list_id}", response=SmartListOut, operation_id="crm_lists_update")
def update_smart_list(
    request: HttpRequest, list_id: uuid.UUID, payload: SmartListPatch
) -> SmartListOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    row = smart_lists.update(
        membership=membership,
        actor=_actor(membership),
        list_id=list_id,
        data=payload.model_dump(exclude_unset=True),
    )
    return _list_out(row)


@router.delete("/smart-lists/{list_id}", response={204: None}, operation_id="crm_lists_delete")
def delete_smart_list(request: HttpRequest, list_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    smart_lists.delete(membership=membership, actor=_actor(membership), list_id=list_id)
    return Status(204, None)


@router.get(
    "/smart-lists/{list_id}/contacts", response=Page[ContactOut], operation_id="crm_lists_contacts"
)
def smart_list_contacts(
    request: HttpRequest,
    list_id: uuid.UUID,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[ContactOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    row = smart_lists.get(membership, list_id)
    page, next_cursor = paginate(
        smart_lists.contacts_of(membership, row, text=q), cursor=cursor, limit=limit
    )
    items = contacts.serialise_contacts(membership.workspace_id, page)
    return Page[ContactOut](items=[ContactOut(**i) for i in items], next_cursor=next_cursor)


# ---- imports ---------------------------------------------------------------------------------


class ImportOut(Schema):
    id: uuid.UUID
    file_name: str
    status: str
    headers: list[str]
    mapping: dict[str, str]
    total_rows: int
    processed_rows: int
    created_count: int
    updated_count: int
    failed_count: int
    created_at: datetime
    finished_at: datetime | None


class ImportCreatedOut(ImportOut):
    preview: list[dict[str, str]]


class MappingIn(Schema):
    mapping: dict[str, str]


class StartIn(Schema):
    mapping: dict[str, str]
    options: dict[str, Any] = {}


def _import_out(job: ImportJob) -> ImportOut:
    return ImportOut.model_validate(job, from_attributes=True)


@router.post("/imports", response={201: ImportCreatedOut}, operation_id="crm_imports_create")
def create_import(request: HttpRequest, file: File[UploadedFile]) -> Status[ImportCreatedOut]:
    membership = resolve_workspace(request, Permission.CRM_IMPORT_EXPORT)
    job = imports.create_import(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        file_name=file.name or "import.csv",
        raw=file.read(),
    )
    body = _import_out(job).model_dump()
    return Status(201, ImportCreatedOut(**body, preview=imports.preview(job)))


@router.get("/imports/{job_id}", response=ImportOut, operation_id="crm_imports_get")
def get_import(request: HttpRequest, job_id: uuid.UUID) -> ImportOut:
    membership = resolve_workspace(request, Permission.CRM_IMPORT_EXPORT)
    return _import_out(imports.get_job(membership.workspace_id, job_id))


@router.post(
    "/imports/{job_id}/dry-run", response=dict[str, Any], operation_id="crm_imports_dry_run"
)
def dry_run(request: HttpRequest, job_id: uuid.UUID, payload: MappingIn) -> dict[str, Any]:
    membership = resolve_workspace(request, Permission.CRM_IMPORT_EXPORT)
    job = imports.get_job(membership.workspace_id, job_id)
    return imports.dry_run(workspace_id=membership.workspace_id, job=job, mapping=payload.mapping)


@router.post("/imports/{job_id}/start", response=ImportOut, operation_id="crm_imports_start")
def start_import(request: HttpRequest, job_id: uuid.UUID, payload: StartIn) -> ImportOut:
    membership = resolve_workspace(request, Permission.CRM_IMPORT_EXPORT)
    job = imports.start(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        job_id=job_id,
        mapping=payload.mapping,
        options=payload.options,
    )
    return _import_out(imports.get_job(membership.workspace_id, job.pk))


@router.get("/imports/{job_id}/errors.csv", operation_id="crm_imports_errors")
def import_errors(request: HttpRequest, job_id: uuid.UUID) -> HttpResponse:
    membership = resolve_workspace_for_file(request, Permission.CRM_IMPORT_EXPORT)
    job = imports.get_job(membership.workspace_id, job_id)
    response = HttpResponse("".join(csvio.write(imports.error_rows(job))), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="import-errors.csv"'
    return response


# ---- export ----------------------------------------------------------------------------------


class ExportQuery(Schema):
    smart_list_id: uuid.UUID | None = None
    q: str | None = None
    columns: str | None = None


@router.get("/contacts-export.csv", operation_id="crm_contacts_export")
def export_contacts(request: HttpRequest, query: Query[ExportQuery]) -> StreamingHttpResponse:
    membership = resolve_workspace_for_file(request, Permission.CRM_IMPORT_EXPORT)
    spec = smart_lists.get(membership, query.smart_list_id).filter if query.smart_list_id else None
    columns = [c for c in (query.columns or "").split(",") if c] or None
    rows = export.rows(
        membership=membership, actor=_actor(membership), spec=spec, text=query.q, columns=columns
    )
    response = StreamingHttpResponse(csvio.write(rows), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="contacts.csv"'
    return response


# ---- bulk actions ----------------------------------------------------------------------------


class ListBulkIn(Schema):
    action: str
    params: dict[str, Any] = {}
    filter: dict[str, Any] | None = None
    q: str | None = None
    contact_ids: list[uuid.UUID] | None = None


class ListBulkOut(Schema):
    id: uuid.UUID
    action: str
    status: str
    total: int
    processed: int
    succeeded: int
    failed: int
    failures: list[dict[str, Any]]
    cancel_requested: bool
    created_at: datetime
    finished_at: datetime | None


def _bulk_out(job: BulkJob) -> ListBulkOut:
    return ListBulkOut.model_validate(job, from_attributes=True)


@router.post("/bulk-actions", response={201: ListBulkOut}, operation_id="crm_bulk_create")
def create_bulk(request: HttpRequest, payload: ListBulkIn) -> Status[ListBulkOut]:
    if payload.action not in BulkAction.values:
        raise ValidationFailedError("invalid action", errors={"action": "invalid_value"})
    membership = resolve_workspace(request, BULK_PERMISSION[BulkAction(payload.action)])
    job = bulk.create(
        membership=membership,
        actor=_actor(membership),
        action=payload.action,
        params=payload.params,
        spec=payload.filter,
        text=payload.q,
        contact_ids=payload.contact_ids,
    )
    return Status(201, _bulk_out(bulk.get_job(membership.workspace_id, job.pk)))


@router.get("/bulk-actions/{job_id}", response=ListBulkOut, operation_id="crm_bulk_get")
def get_bulk(request: HttpRequest, job_id: uuid.UUID) -> ListBulkOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return _bulk_out(bulk.get_job(membership.workspace_id, job_id))


@router.post("/bulk-actions/{job_id}/cancel", response=ListBulkOut, operation_id="crm_bulk_cancel")
def cancel_bulk(request: HttpRequest, job_id: uuid.UUID) -> ListBulkOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    return _bulk_out(bulk.cancel(workspace_id=membership.workspace_id, job_id=job_id))
