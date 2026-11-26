"""Notes and tasks: use cases (public surface of the module)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import Count, Min, Q
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from crm.activity import events, stats
from crm.activity.models import Note, Task, TaskStatus
from crm.contacts import services as contacts
from crm.events.publisher import publish
from crm.pipelines import services as pipelines
from workspaces.models import Membership

NOTE_LIMIT = 20_000
TITLE_LIMIT = 200


def _user_id(actor: Actor) -> uuid.UUID | None:
    return uuid.UUID(actor.id) if actor.type == "human" else None


def _members(workspace_id: uuid.UUID, user_ids: list[uuid.UUID], field: str) -> list[uuid.UUID]:
    wanted = set(user_ids)
    found = set(
        Membership.objects.filter(workspace_id=workspace_id, user_id__in=wanted).values_list(
            "user_id", flat=True
        )
    )
    if found != wanted:
        raise ValidationFailedError("not a member", errors={field: "not_a_member"})
    return sorted(wanted)


def _body(text: str) -> str:
    body = text.strip()
    if not body:
        raise ValidationFailedError("body required", errors={"body": "required"})
    if len(body) > NOTE_LIMIT:
        raise ValidationFailedError("body too long", errors={"body": "too_long"})
    return body


# ---- notes ----------------------------------------------------------------------------------


def add_note(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    contact_id: uuid.UUID,
    body: str,
    mentions: list[uuid.UUID] | None = None,
    pinned: bool = False,
    opportunity_id: uuid.UUID | None = None,
) -> Note:
    clean_body = _body(body)
    mentioned = _members(workspace_id, mentions or [], "mentions")
    with transaction.atomic():
        contact = contacts.get_contact(workspace_id, contact_id)
        if opportunity_id is not None:
            deal = pipelines.get_opportunity(workspace_id, opportunity_id)
            if deal.contact_id != contact.pk:
                raise ValidationFailedError(
                    "deal of another contact", errors={"opportunity_id": "not_of_contact"}
                )
        note = Note.objects.create(
            workspace_id=workspace_id,
            contact=contact,
            opportunity_id=opportunity_id,
            author_id=_user_id(actor),
            body=clean_body,
            pinned=pinned,
            mentions=[str(m) for m in mentioned],
        )
        publish(
            "crm.note.added",
            workspace_id=workspace_id,
            actor=actor,
            data=events.NoteAdded(note_id=note.pk, contact_id=contact.pk, mentions=mentioned),
        )
    return note


def get_note(workspace_id: uuid.UUID, note_id: uuid.UUID) -> Note:
    found = Note.objects.for_workspace(workspace_id).filter(pk=note_id).first()
    if found is None:
        raise NotFoundError("note not found")
    return found


def update_note(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    note_id: uuid.UUID,
    body: str | None = None,
    pinned: bool | None = None,
    can_edit_others: bool = False,
) -> Note:
    with transaction.atomic():
        note = get_note(workspace_id, note_id)
        if body is not None:
            if str(note.author_id) != actor.id and not can_edit_others:
                raise PermissionDeniedError("only the author can edit a note")
            note.body = _body(body)
            note.edited_at = timezone.now()
        if pinned is not None:
            note.pinned = pinned
        note.save()
        record_event(
            action="crm.note.updated",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_note",
            target_id=note.pk,
        )
    return note


def delete_note(
    *, workspace_id: uuid.UUID, actor: Actor, note_id: uuid.UUID, can_delete_others: bool = False
) -> None:
    with transaction.atomic():
        note = get_note(workspace_id, note_id)
        if str(note.author_id) != actor.id and not can_delete_others:
            raise PermissionDeniedError("only the author can delete a note")
        note.delete()
        record_event(
            action="crm.note.deleted",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_note",
            target_id=note_id,
        )


# ---- tasks ----------------------------------------------------------------------------------


def _title(text: str) -> str:
    title = text.strip()
    if not title or len(title) > TITLE_LIMIT:
        raise ValidationFailedError(
            "invalid title", errors={"title": "required" if not title else "too_long"}
        )
    return title


def create_task(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    contact_id: uuid.UUID,
    title: str,
    description: str = "",
    due_at: datetime | None = None,
    assigned_to: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
) -> Task:
    clean_title = _title(title)
    if assigned_to is not None:
        _members(workspace_id, [assigned_to], "assigned_to")
    with transaction.atomic():
        contact = contacts.get_contact(workspace_id, contact_id)
        if opportunity_id is not None:
            deal = pipelines.get_opportunity(workspace_id, opportunity_id)
            if deal.contact_id != contact.pk:
                raise ValidationFailedError(
                    "deal of another contact", errors={"opportunity_id": "not_of_contact"}
                )
        task = Task.objects.create(
            workspace_id=workspace_id,
            contact=contact,
            title=clean_title,
            description=description.strip(),
            due_at=due_at,
            assigned_to_id=assigned_to,
            opportunity_id=opportunity_id,
        )
        publish(
            "crm.task.created",
            workspace_id=workspace_id,
            actor=actor,
            data=events.TaskCreated(
                task_id=task.pk, contact_id=contact.pk, assigned_to=assigned_to
            ),
        )
    return task


def get_task(workspace_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    found = Task.objects.for_workspace(workspace_id).filter(pk=task_id).first()
    if found is None:
        raise NotFoundError("task not found")
    return found


def update_task(
    *, workspace_id: uuid.UUID, actor: Actor, task_id: uuid.UUID, data: dict[str, Any]
) -> Task:
    with transaction.atomic():
        task = get_task(workspace_id, task_id)
        if "title" in data:
            task.title = _title(data["title"])
        if "description" in data:
            task.description = str(data["description"] or "").strip()
        if "due_at" in data:
            task.due_at = data["due_at"]
        if "assigned_to" in data:
            if data["assigned_to"] is not None:
                _members(workspace_id, [data["assigned_to"]], "assigned_to")
            task.assigned_to_id = data["assigned_to"]
        task.save()
        record_event(
            action="crm.task.updated",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_task",
            target_id=task.pk,
        )
    return task


def complete_task(*, workspace_id: uuid.UUID, actor: Actor, task_id: uuid.UUID) -> Task:
    with transaction.atomic():
        task = (
            Task.objects.select_for_update().filter(workspace_id=workspace_id, pk=task_id).first()
        )
        if task is None:
            raise NotFoundError("task not found")
        if task.status == TaskStatus.COMPLETE:
            return task
        task.status = TaskStatus.COMPLETE
        task.completed_at = timezone.now()
        task.completed_by_id = _user_id(actor)
        task.save()
        publish(
            "crm.task.completed",
            workspace_id=workspace_id,
            actor=actor,
            data=events.TaskCompleted(task_id=task.pk, contact_id=task.contact_id),
        )
    return task


def reopen_task(*, workspace_id: uuid.UUID, actor: Actor, task_id: uuid.UUID) -> Task:
    with transaction.atomic():
        task = get_task(workspace_id, task_id)
        task.status, task.completed_at, task.completed_by = TaskStatus.INCOMPLETE, None, None
        task.save()
        record_event(
            action="crm.task.reopened",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_task",
            target_id=task.pk,
        )
    return task


def delete_task(*, workspace_id: uuid.UUID, actor: Actor, task_id: uuid.UUID) -> None:
    with transaction.atomic():
        task = get_task(workspace_id, task_id)
        task.delete()
        record_event(
            action="crm.task.deleted",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_task",
            target_id=task_id,
        )


tasks_completed_by_user = stats.tasks_completed_by_user


def open_task_summary(
    workspace_id: uuid.UUID, opportunity_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Open tasks per deal: how many, how many are overdue, and when the next one is due."""
    now = timezone.now()
    rows = (
        Task.objects.for_workspace(workspace_id)
        .filter(opportunity_id__in=opportunity_ids, status=TaskStatus.INCOMPLETE)
        .values("opportunity_id")
        .annotate(
            open=Count("id"),
            overdue=Count("id", filter=Q(due_at__lt=now)),
            next_due_at=Min("due_at"),
        )
    )
    return {
        r["opportunity_id"]: {
            "open": r["open"],
            "overdue": r["overdue"],
            "next_due_at": r["next_due_at"],
        }
        for r in rows
    }
