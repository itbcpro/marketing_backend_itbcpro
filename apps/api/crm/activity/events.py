"""Event data models of the activity module (notes and tasks)."""

import uuid

from pydantic import BaseModel

from crm.events.registry import event


@event("crm.note.added")
class NoteAdded(BaseModel):
    note_id: uuid.UUID
    contact_id: uuid.UUID
    mentions: list[uuid.UUID]


@event("crm.task.created")
class TaskCreated(BaseModel):
    task_id: uuid.UUID
    contact_id: uuid.UUID
    assigned_to: uuid.UUID | None


@event("crm.task.completed")
class TaskCompleted(BaseModel):
    task_id: uuid.UUID
    contact_id: uuid.UUID


@event("crm.appointment.booked")
class AppointmentBooked(BaseModel):
    appointment_id: uuid.UUID
    contact_id: uuid.UUID
    starts_at: str
    assigned_to: uuid.UUID | None


@event("crm.appointment.status_changed")
class AppointmentStatusChanged(BaseModel):
    appointment_id: uuid.UUID
    contact_id: uuid.UUID
    status: str
