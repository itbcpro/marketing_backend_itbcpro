"""Workflows: definitions, publishing, enrolments and the execution log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from core.errors import NotFoundError
from crm.contacts import services as contacts
from crm.workflows import actions, definitions, engine, simulation, triggers
from crm.workflows.models import Enrollment, StepLog, Workflow, WorkflowVersion
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-workflows"], auth=api_auth)


class WorkflowIn(Schema):
    name: str
    reenter: bool = False
    stop_on_reply: bool = True


class WorkflowPatch(Schema):
    name: str | None = None
    reenter: bool | None = None
    stop_on_reply: bool | None = None


class WorkflowOut(Schema):
    id: uuid.UUID
    name: str
    status: str
    reenter: bool
    stop_on_reply: bool
    published_number: int | None
    has_draft: bool


class DefinitionOut(Schema):
    number: int
    published: bool
    definition: dict[str, Any]


class DefinitionIn(Schema):
    definition: dict[str, Any]


class PausedIn(Schema):
    paused: bool


class EnrollIn(Schema):
    contact_id: uuid.UUID


class EnrollmentOut(Schema):
    id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_name: str
    contact_id: uuid.UUID
    status: str
    current_step: str
    wake_at: datetime | None
    exit_reason: str
    created_at: datetime
    finished_at: datetime | None


class StepLogOut(Schema):
    step_id: str
    step_type: str
    outcome: str
    detail: str
    attempt: int
    created_at: datetime


class EntryOut(Schema):
    key: str
    config_schema: dict[str, Any]
    event_type: str | None = None


class CatalogOut(Schema):
    triggers: list[EntryOut]
    actions: list[EntryOut]


def _workflow(row: Workflow) -> WorkflowOut:
    latest = row.versions.order_by("-number").first()
    return WorkflowOut(
        id=row.pk,
        name=row.name,
        status=row.status,
        reenter=row.reenter,
        stop_on_reply=row.stop_on_reply,
        published_number=row.published_version.number if row.published_version else None,
        has_draft=bool(latest and latest.published_at is None),
    )


def _enrollment(row: Enrollment) -> EnrollmentOut:
    return EnrollmentOut(
        id=row.pk,
        workflow_id=row.workflow_id,
        workflow_name=row.workflow.name,
        contact_id=row.contact_id,
        status=row.status,
        current_step=row.current_step,
        wake_at=row.wake_at,
        exit_reason=row.exit_reason,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


def _version(row: WorkflowVersion) -> DefinitionOut:
    return DefinitionOut(
        number=row.number, published=row.published_at is not None, definition=row.definition
    )


@router.get("/workflows/catalog", response=CatalogOut, operation_id="crm_workflow_catalog")
def catalog(request: HttpRequest) -> CatalogOut:
    """Every trigger and action the builder may offer, with the shape of its settings."""
    resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    return CatalogOut(
        triggers=[
            EntryOut(
                key=t.key, config_schema=t.config_model.model_json_schema(), event_type=t.event_type
            )
            for t in triggers.all_triggers()
        ],
        actions=[
            EntryOut(key=a.key, config_schema=a.config_model.model_json_schema())
            for a in actions.all_actions()
        ],
    )


@router.get("/workflows", response=list[WorkflowOut], operation_id="crm_workflows")
def list_workflows(request: HttpRequest) -> list[WorkflowOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    return [_workflow(w) for w in definitions.list_workflows(m.workspace_id)]


@router.post("/workflows", response={201: WorkflowOut}, operation_id="crm_workflow_create")
def create_workflow(request: HttpRequest, payload: WorkflowIn) -> Status[WorkflowOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    return Status(
        201, _workflow(definitions.create_workflow(m.workspace_id, **payload.model_dump()))
    )


@router.patch("/workflows/{workflow_id}", response=WorkflowOut, operation_id="crm_workflow_update")
def update_workflow(
    request: HttpRequest, workflow_id: uuid.UUID, payload: WorkflowPatch
) -> WorkflowOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    row = definitions.update_settings(m.workspace_id, workflow_id, **payload.model_dump())
    return _workflow(row)


@router.delete(
    "/workflows/{workflow_id}", response={204: None}, operation_id="crm_workflow_archive"
)
def archive_workflow(request: HttpRequest, workflow_id: uuid.UUID) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    definitions.archive(m.workspace_id, workflow_id)
    return Status(204, None)


@router.get(
    "/workflows/{workflow_id}/definition",
    response=DefinitionOut,
    operation_id="crm_workflow_definition",
)
def get_definition(request: HttpRequest, workflow_id: uuid.UUID) -> DefinitionOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    workflow = definitions.get_workflow(m.workspace_id, workflow_id)
    latest = definitions.latest_version(workflow)
    if latest is None:
        raise NotFoundError("no definition yet")
    return _version(latest)


@router.put(
    "/workflows/{workflow_id}/definition",
    response=DefinitionOut,
    operation_id="crm_workflow_save_draft",
)
def save_draft(
    request: HttpRequest, workflow_id: uuid.UUID, payload: DefinitionIn
) -> DefinitionOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    return _version(definitions.save_draft(m.workspace_id, workflow_id, payload.definition))


@router.post(
    "/workflows/{workflow_id}/publish", response=WorkflowOut, operation_id="crm_workflow_publish"
)
def publish(request: HttpRequest, workflow_id: uuid.UUID) -> WorkflowOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    return _workflow(definitions.publish(m.workspace_id, workflow_id))


@router.post(
    "/workflows/{workflow_id}/paused", response=WorkflowOut, operation_id="crm_workflow_paused"
)
def set_paused(request: HttpRequest, workflow_id: uuid.UUID, payload: PausedIn) -> WorkflowOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    return _workflow(definitions.set_paused(m.workspace_id, workflow_id, payload.paused))


@router.post(
    "/workflows/{workflow_id}/enroll",
    response={201: EnrollmentOut, 200: None},
    operation_id="crm_workflow_enroll",
)
def enroll(request: HttpRequest, workflow_id: uuid.UUID, payload: EnrollIn) -> Any:
    """Put a contact in by hand. A 200 with no body means the rules kept them out."""
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    contacts.get_visible_contact(m, payload.contact_id)
    row = engine.enroll(m.workspace_id, workflow_id, payload.contact_id, source="manual")
    if row is None:
        return Status(200, None)
    return Status(201, _enrollment(row))


@router.get(
    "/workflows/{workflow_id}/enrollments",
    response=list[EnrollmentOut],
    operation_id="crm_workflow_enrollments",
)
def workflow_enrollments(
    request: HttpRequest, workflow_id: uuid.UUID, limit: int = 50
) -> list[EnrollmentOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    definitions.get_workflow(m.workspace_id, workflow_id)
    rows = Enrollment.objects.for_workspace(m.workspace_id).filter(workflow_id=workflow_id)
    return [_enrollment(r) for r in rows.select_related("workflow")[: max(1, min(limit, 200))]]


@router.get(
    "/contacts/{contact_id}/enrollments",
    response=list[EnrollmentOut],
    operation_id="crm_contact_enrollments",
)
def contact_enrollments(request: HttpRequest, contact_id: uuid.UUID) -> list[EnrollmentOut]:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    contacts.get_visible_contact(m, contact_id)
    rows = Enrollment.objects.for_workspace(m.workspace_id).filter(contact_id=contact_id)
    return [_enrollment(r) for r in rows.select_related("workflow")[:100]]


@router.get(
    "/enrollments/{enrollment_id}/log",
    response=list[StepLogOut],
    operation_id="crm_enrollment_log",
)
def enrollment_log(request: HttpRequest, enrollment_id: uuid.UUID) -> list[StepLogOut]:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    row = Enrollment.objects.for_workspace(m.workspace_id).filter(pk=enrollment_id).first()
    if row is None:
        raise NotFoundError("enrollment not found")
    contacts.get_visible_contact(m, row.contact_id)
    logs = StepLog.objects.for_workspace(m.workspace_id).filter(enrollment=row)
    return [
        StepLogOut(
            step_id=entry.step_id,
            step_type=entry.step_type,
            outcome=entry.outcome,
            detail=entry.detail,
            attempt=entry.attempt,
            created_at=entry.created_at,
        )
        for entry in logs
    ]


@router.post(
    "/enrollments/{enrollment_id}/exit",
    response={204: None},
    operation_id="crm_enrollment_exit",
)
def exit_enrollment(request: HttpRequest, enrollment_id: uuid.UUID) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    row = Enrollment.objects.for_workspace(m.workspace_id).filter(pk=enrollment_id).first()
    if row is None:
        raise NotFoundError("enrollment not found")
    engine.exit_enrollment(row, "removed")
    return Status(204, None)


class SimulateIn(Schema):
    contact_id: uuid.UUID
    definition: dict[str, Any] | None = None


class VisitOut(Schema):
    step_id: str
    step_type: str
    note: str


@router.post(
    "/workflows/{workflow_id}/simulate",
    response=list[VisitOut],
    operation_id="crm_workflow_simulate",
)
def simulate(request: HttpRequest, workflow_id: uuid.UUID, payload: SimulateIn) -> list[VisitOut]:
    """Walk the given (or the latest saved) definition for one contact; nothing is sent."""
    m = resolve_workspace(request, Permission.CRM_MANAGE_AUTOMATION)
    workflow = definitions.get_workflow(m.workspace_id, workflow_id)
    contacts.get_visible_contact(m, payload.contact_id)
    body = payload.definition
    if body is None:
        latest = definitions.latest_version(workflow)
        if latest is None:
            raise NotFoundError("no definition yet")
        body = latest.definition
    definitions.validate(body)
    visits = simulation.simulate(m.workspace_id, body, payload.contact_id)
    return [VisitOut(step_id=v.step_id, step_type=v.step_type, note=v.note) for v in visits]
