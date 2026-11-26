"""Custom fields and workspace values."""

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Schema, Status

from audit.actors import Actor
from core.errors import ValidationFailedError
from crm.fields import services
from crm.fields.models import FieldDefinition, ObjectType, WorkspaceValue
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-fields"], auth=api_auth)


class FieldIn(Schema):
    object_type: str
    key: str
    label: str
    field_type: str
    options: list[dict[str, str]] = []
    folder: str = ""


class FieldPatch(Schema):
    label: str | None = None
    options: list[dict[str, str]] | None = None
    folder: str | None = None


class FieldOut(Schema):
    id: uuid.UUID
    object_type: str
    key: str
    label: str
    field_type: str
    options: list[dict[str, str]]
    folder: str
    position: int
    created_at: datetime
    archived_at: datetime | None


class FieldFilters(Schema):
    object_type: str = ObjectType.CONTACT
    include_archived: bool = False


class FolderRenameIn(Schema):
    object_type: str
    old: str
    new: str


class FolderRenameOut(Schema):
    changed: int


class ReorderIn(Schema):
    object_type: str
    ordered_ids: list[uuid.UUID]


class WorkspaceValueIn(Schema):
    label: str
    value: str = ""


class WorkspaceValueOut(Schema):
    key: str
    label: str
    value: str


def _out(row: FieldDefinition) -> FieldOut:
    return FieldOut.model_validate(row, from_attributes=True)


@router.get("/fields", response=list[FieldOut], operation_id="crm_fields_list")
def list_fields(request: HttpRequest, filters: Query[FieldFilters]) -> list[FieldOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    if filters.object_type not in ObjectType.values:
        raise ValidationFailedError("invalid object type")
    rows = services.list_definitions(
        membership.workspace_id, filters.object_type, include_archived=filters.include_archived
    )
    return [_out(r) for r in rows]


@router.post("/fields", response={201: FieldOut}, operation_id="crm_fields_create")
def create_field(request: HttpRequest, payload: FieldIn) -> Status[FieldOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    row = services.create_definition(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        **payload.model_dump(),
    )
    return Status(201, _out(row))


@router.post("/fields/reorder", response={204: None}, operation_id="crm_fields_reorder")
def reorder_fields(request: HttpRequest, payload: ReorderIn) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    services.reorder(
        workspace_id=membership.workspace_id,
        object_type=payload.object_type,
        ordered_ids=payload.ordered_ids,
    )
    return Status(204, None)


@router.post(
    "/fields/folders/rename", response=FolderRenameOut, operation_id="crm_fields_folder_rename"
)
def rename_folder(request: HttpRequest, payload: FolderRenameIn) -> dict[str, int]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    changed = services.rename_folder(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        object_type=payload.object_type,
        old=payload.old,
        new=payload.new,
    )
    return {"changed": changed}


@router.patch("/fields/{field_id}", response=FieldOut, operation_id="crm_fields_update")
def update_field(request: HttpRequest, field_id: uuid.UUID, payload: FieldPatch) -> FieldOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    row = services.update_definition(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        field_id=field_id,
        **payload.model_dump(exclude_unset=True),
    )
    return _out(row)


@router.post("/fields/{field_id}/archive", response={204: None}, operation_id="crm_fields_archive")
def archive_field(request: HttpRequest, field_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    services.archive_definition(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        field_id=field_id,
    )
    return Status(204, None)


@router.get("/workspace-values", response=list[WorkspaceValueOut], operation_id="crm_values_list")
def list_workspace_values(request: HttpRequest) -> list[dict[str, Any]]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = WorkspaceValue.objects.for_workspace(membership.workspace_id).order_by("key")
    return [{"key": r.key, "label": r.label, "value": r.value} for r in rows]


@router.put("/workspace-values/{key}", response=WorkspaceValueOut, operation_id="crm_values_put")
def put_workspace_value(
    request: HttpRequest, key: str, payload: WorkspaceValueIn
) -> dict[str, str]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    row = services.set_workspace_value(
        workspace_id=membership.workspace_id, key=key, label=payload.label, value=payload.value
    )
    return {"key": row.key, "label": row.label, "value": row.value}


@router.delete("/workspace-values/{key}", response={204: None}, operation_id="crm_values_delete")
def delete_workspace_value(request: HttpRequest, key: str) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    services.delete_workspace_value(workspace_id=membership.workspace_id, key=key)
    return Status(204, None)
