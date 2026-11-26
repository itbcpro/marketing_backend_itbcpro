"""Notes, tasks and the timeline of a contact."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, Page, decode_cursor, encode_cursor
from core.errors import NotFoundError, ValidationFailedError
from crm.activity import services
from crm.activity.models import Activity, Note, Task, TaskStatus
from crm.contacts import services as contacts
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.models import Membership, RecordScope
from workspaces.permissions import Permission, role_allows

router = Router(tags=["crm-activity"], auth=api_auth)
TASK_SCOPES = ("mine", "overdue", "today", "open", "all")


class NoteIn(Schema):
    body: str
    opportunity_id: uuid.UUID | None = None
    mentions: list[uuid.UUID] = []
    pinned: bool = False


class NotePatch(Schema):
    body: str | None = None
    pinned: bool | None = None


class NoteOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    opportunity_id: uuid.UUID | None
    author_id: uuid.UUID | None
    body: str
    pinned: bool
    mentions: list[str]
    created_at: datetime
    edited_at: datetime | None


class TaskIn(Schema):
    title: str
    description: str = ""
    due_at: datetime | None = None
    assigned_to: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None


class TaskPatch(Schema):
    title: str | None = None
    description: str | None = None
    due_at: datetime | None = None
    assigned_to: uuid.UUID | None = None


class TaskOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str = ""
    opportunity_id: uuid.UUID | None
    title: str
    description: str
    due_at: datetime | None
    assigned_to_id: uuid.UUID | None
    status: str
    completed_at: datetime | None
    created_at: datetime


class ActivityOut(Schema):
    id: uuid.UUID
    kind: str
    data: dict[str, Any]
    actor_type: str
    actor_id: str
    occurred_at: datetime


def _actor(membership: Membership) -> Actor:
    return Actor.human(membership.user_id)


# ---- timeline -------------------------------------------------------------------------------


@router.get(
    "/contacts/{contact_id}/timeline", response=Page[ActivityOut], operation_id="crm_timeline"
)
def timeline(
    request: HttpRequest,
    contact_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[ActivityOut]:
    """Newest first. The cursor is the id of the last item of the previous page."""
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    contacts.get_visible_contact(membership, contact_id)
    rows = Activity.objects.for_workspace(membership.workspace_id).filter(contact_id=contact_id)
    size = max(1, min(limit, MAX_LIMIT))
    ordered = rows.order_by("-pk")
    if cursor:
        ordered = ordered.filter(pk__lt=decode_cursor(cursor))
    page = list(ordered[: size + 1])
    next_cursor = encode_cursor(page[size - 1].pk) if len(page) > size else None
    return Page[ActivityOut](
        items=[ActivityOut.model_validate(r, from_attributes=True) for r in page[:size]],
        next_cursor=next_cursor,
    )


# ---- notes ----------------------------------------------------------------------------------


@router.get("/contacts/{contact_id}/notes", response=list[NoteOut], operation_id="crm_notes_list")
def list_notes(
    request: HttpRequest, contact_id: uuid.UUID, opportunity_id: uuid.UUID | None = None
) -> list[NoteOut]:
    """A contact's notes; with `opportunity_id`, only the notes about that deal."""
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    contacts.get_visible_contact(membership, contact_id)
    rows = Note.objects.for_workspace(membership.workspace_id).filter(contact_id=contact_id)
    if opportunity_id is not None:
        rows = rows.filter(opportunity_id=opportunity_id)
    return [NoteOut.model_validate(n, from_attributes=True) for n in rows]


@router.post(
    "/contacts/{contact_id}/notes", response={201: NoteOut}, operation_id="crm_notes_create"
)
def create_note(request: HttpRequest, contact_id: uuid.UUID, payload: NoteIn) -> Status[NoteOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    contacts.get_visible_contact(membership, contact_id)
    note = services.add_note(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        contact_id=contact_id,
        body=payload.body,
        mentions=payload.mentions,
        pinned=payload.pinned,
        opportunity_id=payload.opportunity_id,
    )
    return Status(201, NoteOut.model_validate(note, from_attributes=True))


def _visible_note(membership: Membership, note_id: uuid.UUID) -> Note:
    note = services.get_note(membership.workspace_id, note_id)
    contacts.get_visible_contact(membership, note.contact_id)
    return note


@router.patch("/notes/{note_id}", response=NoteOut, operation_id="crm_notes_update")
def update_note(request: HttpRequest, note_id: uuid.UUID, payload: NotePatch) -> NoteOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_note(membership, note_id)
    note = services.update_note(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        note_id=note_id,
        body=payload.body,
        pinned=payload.pinned,
        can_edit_others=role_allows(membership.role, Permission.CRM_MANAGE_SETTINGS),
    )
    return NoteOut.model_validate(note, from_attributes=True)


@router.delete("/notes/{note_id}", response={204: None}, operation_id="crm_notes_delete")
def delete_note(request: HttpRequest, note_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_note(membership, note_id)
    services.delete_note(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        note_id=note_id,
        can_delete_others=role_allows(membership.role, Permission.CRM_DELETE),
    )
    return Status(204, None)


# ---- tasks ----------------------------------------------------------------------------------


def _visible_tasks(membership: Membership) -> QuerySet[Task]:
    rows = Task.objects.for_workspace(membership.workspace_id).select_related("contact")
    if membership.record_scope == RecordScope.ASSIGNED:
        rows = rows.filter(
            Q(assigned_to_id=membership.user_id) | Q(contact__assigned_to_id=membership.user_id)
        )
    return rows


def _task_out(task: Task) -> TaskOut:
    out = TaskOut.model_validate(task, from_attributes=True)
    out.contact_name = str(task.contact)
    return out


@router.get("/tasks", response=Page[TaskOut], operation_id="crm_tasks_list")
def list_tasks(
    request: HttpRequest,
    scope: str = "open",
    contact_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[TaskOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    if scope not in TASK_SCOPES:
        raise ValidationFailedError("invalid scope", errors={"scope": "invalid_value"})
    now = timezone.now()
    rows = _visible_tasks(membership)
    if contact_id:
        rows = rows.filter(contact_id=contact_id)
    if opportunity_id:
        rows = rows.filter(opportunity_id=opportunity_id)
    if scope != "all":
        rows = rows.filter(status=TaskStatus.INCOMPLETE)
    if scope == "mine":
        rows = rows.filter(assigned_to_id=membership.user_id)
    elif scope == "overdue":
        rows = rows.filter(due_at__lt=now)
    elif scope == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = rows.filter(due_at__gte=start, due_at__lt=start + timedelta(days=1))
    size = max(1, min(limit, MAX_LIMIT))
    ordered = rows.order_by("pk")
    if cursor:
        ordered = ordered.filter(pk__gt=decode_cursor(cursor))
    page = list(ordered[: size + 1])
    next_cursor = encode_cursor(page[size - 1].pk) if len(page) > size else None
    return Page[TaskOut](items=[_task_out(t) for t in page[:size]], next_cursor=next_cursor)


@router.post(
    "/contacts/{contact_id}/tasks", response={201: TaskOut}, operation_id="crm_tasks_create"
)
def create_task(request: HttpRequest, contact_id: uuid.UUID, payload: TaskIn) -> Status[TaskOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    contacts.get_visible_contact(membership, contact_id)
    task = services.create_task(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        contact_id=contact_id,
        **payload.model_dump(),
    )
    return Status(201, _task_out(task))


def _visible_task(membership: Membership, task_id: uuid.UUID) -> Task:
    found = _visible_tasks(membership).filter(pk=task_id).first()
    if found is None:
        raise NotFoundError("task not found")
    return found


@router.patch("/tasks/{task_id}", response=TaskOut, operation_id="crm_tasks_update")
def update_task(request: HttpRequest, task_id: uuid.UUID, payload: TaskPatch) -> TaskOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_task(membership, task_id)
    task = services.update_task(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        task_id=task_id,
        data=payload.model_dump(exclude_unset=True),
    )
    return _task_out(task)


@router.post("/tasks/{task_id}/complete", response=TaskOut, operation_id="crm_tasks_complete")
def complete_task(request: HttpRequest, task_id: uuid.UUID) -> TaskOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_task(membership, task_id)
    return _task_out(
        services.complete_task(
            workspace_id=membership.workspace_id, actor=_actor(membership), task_id=task_id
        )
    )


@router.post("/tasks/{task_id}/reopen", response=TaskOut, operation_id="crm_tasks_reopen")
def reopen_task(request: HttpRequest, task_id: uuid.UUID) -> TaskOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_task(membership, task_id)
    return _task_out(
        services.reopen_task(
            workspace_id=membership.workspace_id, actor=_actor(membership), task_id=task_id
        )
    )


@router.delete("/tasks/{task_id}", response={204: None}, operation_id="crm_tasks_delete")
def delete_task(request: HttpRequest, task_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible_task(membership, task_id)
    services.delete_task(
        workspace_id=membership.workspace_id, actor=_actor(membership), task_id=task_id
    )
    return Status(204, None)
