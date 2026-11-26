"""Message templates and snippets."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from crm.contacts import services as contacts
from crm.templates import services
from crm.templates.models import MessageTemplate, TemplateKind
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-templates"], auth=api_auth)


class TemplateIn(Schema):
    name: str
    body: str
    kind: str = TemplateKind.TEMPLATE
    channel: str = ""
    subject: str = ""


class TemplatePatch(Schema):
    name: str | None = None
    body: str | None = None
    kind: str | None = None
    channel: str | None = None
    subject: str | None = None


class TemplateOut(Schema):
    id: uuid.UUID
    name: str
    kind: str
    channel: str
    subject: str
    body: str
    created_at: datetime
    updated_at: datetime


class PreviewIn(Schema):
    contact_id: uuid.UUID
    subject: str | None = None
    body: str | None = None


class PreviewOut(Schema):
    subject: str
    body: str


class FieldsOut(Schema):
    namespaces: dict[str, list[str]]


def _out(row: MessageTemplate) -> TemplateOut:
    return TemplateOut.model_validate(row, from_attributes=True)


@router.get("/templates", response=list[TemplateOut], operation_id="crm_templates_list")
def list_templates(
    request: HttpRequest, kind: str | None = None, channel: str | None = None
) -> list[TemplateOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = services.list_templates(membership.workspace_id, kind=kind, channel=channel)
    return [_out(r) for r in rows]


@router.get("/template-fields", response=FieldsOut, operation_id="crm_template_fields")
def merge_fields(request: HttpRequest) -> FieldsOut:
    """Every merge field the workspace can use, for the editor's picker."""
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    known = services.known_keys(membership.workspace_id)
    return FieldsOut(namespaces={ns: sorted(keys) for ns, keys in known.items()})


@router.post("/templates", response={201: TemplateOut}, operation_id="crm_templates_create")
def create_template(request: HttpRequest, payload: TemplateIn) -> Status[TemplateOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    row = services.create(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        **payload.model_dump(),
    )
    return Status(201, _out(row))


@router.get("/templates/{template_id}", response=TemplateOut, operation_id="crm_templates_get")
def get_template(request: HttpRequest, template_id: uuid.UUID) -> TemplateOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return _out(services.get(membership.workspace_id, template_id))


@router.patch("/templates/{template_id}", response=TemplateOut, operation_id="crm_templates_update")
def update_template(
    request: HttpRequest, template_id: uuid.UUID, payload: TemplatePatch
) -> TemplateOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    row = services.update(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        template_id=template_id,
        data=payload.model_dump(exclude_unset=True, exclude_none=True),
    )
    return _out(row)


@router.delete(
    "/templates/{template_id}", response={204: None}, operation_id="crm_templates_archive"
)
def archive_template(request: HttpRequest, template_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    services.archive(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        template_id=template_id,
    )
    return Status(204, None)


@router.post(
    "/templates/{template_id}/preview", response=PreviewOut, operation_id="crm_templates_preview"
)
def preview(request: HttpRequest, template_id: uuid.UUID, payload: PreviewIn) -> PreviewOut:
    """Render with a real contact's data (the contact must be visible to the caller)."""
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    contacts.get_visible_contact(membership, payload.contact_id)
    row = services.get(membership.workspace_id, template_id)
    rendered = services.render_text(
        membership.workspace_id,
        payload.contact_id,
        subject=row.subject if payload.subject is None else payload.subject,
        body=row.body if payload.body is None else payload.body,
    )
    return PreviewOut(subject=rendered.subject, body=rendered.body)
