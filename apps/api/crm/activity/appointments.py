"""Appointments with a contact: use cases (re-exported by ``services``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import NotFoundError, ValidationFailedError
from crm.activity import events
from crm.activity.models import Appointment, AppointmentStatus
from crm.contacts import services as contacts
from crm.events.publisher import publish
from crm.pipelines import services as pipelines
from workspaces.models import Membership, RecordScope

TITLE_LIMIT = 200
LOCATION_LIMIT = 300
NOTES_LIMIT = 5_000
SCOPES = ("upcoming", "past", "all")


def _text(value: str, field: str, limit: int, *, required: bool = False) -> str:
    clean = value.strip()
    if required and not clean:
        raise ValidationFailedError("required", errors={field: "required"})
    if len(clean) > limit:
        raise ValidationFailedError("too long", errors={field: "too_long"})
    return clean


def _window(starts_at: datetime, ends_at: datetime) -> None:
    if timezone.is_naive(starts_at) or timezone.is_naive(ends_at):
        raise ValidationFailedError("time zone required", errors={"starts_at": "invalid_value"})
    if ends_at <= starts_at:
        raise ValidationFailedError("ends before it starts", errors={"ends_at": "before_start"})


def _assignee(workspace_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    if user_id is None:
        return
    found = Membership.objects.filter(workspace_id=workspace_id, user_id=user_id).exists()
    if not found:
        raise ValidationFailedError("not a member", errors={"assigned_to": "not_a_member"})


def visible(membership: Membership) -> QuerySet[Appointment]:
    rows = Appointment.objects.for_workspace(membership.workspace_id).select_related("contact")
    if membership.record_scope == RecordScope.ASSIGNED:
        rows = rows.filter(
            Q(assigned_to_id=membership.user_id) | Q(contact__assigned_to_id=membership.user_id)
        )
    return rows


def listing(
    membership: Membership,
    *,
    scope: str = "upcoming",
    contact_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
) -> QuerySet[Appointment]:
    if scope not in SCOPES:
        raise ValidationFailedError("invalid scope", errors={"scope": "invalid_value"})
    rows = visible(membership)
    if contact_id:
        rows = rows.filter(contact_id=contact_id)
    if opportunity_id:
        rows = rows.filter(opportunity_id=opportunity_id)
    now = timezone.now()
    if scope == "upcoming":
        return rows.filter(ends_at__gte=now).order_by("starts_at")
    if scope == "past":
        return rows.filter(ends_at__lt=now).order_by("-starts_at")
    return rows.order_by("starts_at")


def get_appointment(workspace_id: uuid.UUID, appointment_id: uuid.UUID) -> Appointment:
    found = (
        Appointment.objects.for_workspace(workspace_id)
        .select_related("contact")
        .filter(pk=appointment_id)
        .first()
    )
    if found is None:
        raise NotFoundError("appointment not found")
    return found


def book(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    contact_id: uuid.UUID,
    title: str,
    starts_at: datetime,
    ends_at: datetime,
    location: str = "",
    notes: str = "",
    assigned_to: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
) -> Appointment:
    clean_title = _text(title, "title", TITLE_LIMIT, required=True)
    _window(starts_at, ends_at)
    _assignee(workspace_id, assigned_to)
    with transaction.atomic():
        contact = contacts.get_contact(workspace_id, contact_id)
        if opportunity_id is not None:
            deal = pipelines.get_opportunity(workspace_id, opportunity_id)
            if deal.contact_id != contact.pk:
                raise ValidationFailedError(
                    "deal of another contact", errors={"opportunity_id": "not_of_contact"}
                )
        appointment = Appointment.objects.create(
            workspace_id=workspace_id,
            contact=contact,
            opportunity_id=opportunity_id,
            title=clean_title,
            starts_at=starts_at,
            ends_at=ends_at,
            location=_text(location, "location", LOCATION_LIMIT),
            notes=_text(notes, "notes", NOTES_LIMIT),
            assigned_to_id=assigned_to,
        )
        publish(
            "crm.appointment.booked",
            workspace_id=workspace_id,
            actor=actor,
            data=events.AppointmentBooked(
                appointment_id=appointment.pk,
                contact_id=contact.pk,
                starts_at=starts_at.isoformat(),
                assigned_to=assigned_to,
            ),
        )
    return appointment


def update(
    *, workspace_id: uuid.UUID, actor: Actor, appointment_id: uuid.UUID, data: dict[str, Any]
) -> Appointment:
    with transaction.atomic():
        appointment = get_appointment(workspace_id, appointment_id)
        if "title" in data:
            appointment.title = _text(data["title"] or "", "title", TITLE_LIMIT, required=True)
        if "location" in data:
            appointment.location = _text(data["location"] or "", "location", LOCATION_LIMIT)
        if "notes" in data:
            appointment.notes = _text(data["notes"] or "", "notes", NOTES_LIMIT)
        if "assigned_to" in data:
            _assignee(workspace_id, data["assigned_to"])
            appointment.assigned_to_id = data["assigned_to"]
        starts_at = data.get("starts_at") or appointment.starts_at
        ends_at = data.get("ends_at") or appointment.ends_at
        _window(starts_at, ends_at)
        appointment.starts_at, appointment.ends_at = starts_at, ends_at
        appointment.save()
        record_event(
            action="crm.appointment.updated",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_appointment",
            target_id=appointment.pk,
        )
    return appointment


def set_status(
    *, workspace_id: uuid.UUID, actor: Actor, appointment_id: uuid.UUID, status: str
) -> Appointment:
    if status not in AppointmentStatus.values:
        raise ValidationFailedError("invalid status", errors={"status": "invalid_value"})
    with transaction.atomic():
        appointment = get_appointment(workspace_id, appointment_id)
        if appointment.status != status:
            appointment.status = status
            appointment.save(update_fields=["status", "updated_at"])
            publish(
                "crm.appointment.status_changed",
                workspace_id=workspace_id,
                actor=actor,
                data=events.AppointmentStatusChanged(
                    appointment_id=appointment.pk,
                    contact_id=appointment.contact_id,
                    status=status,
                ),
            )
    return appointment


def delete(*, workspace_id: uuid.UUID, actor: Actor, appointment_id: uuid.UUID) -> None:
    with transaction.atomic():
        appointment = get_appointment(workspace_id, appointment_id)
        appointment.delete()
        record_event(
            action="crm.appointment.deleted",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_appointment",
            target_id=appointment_id,
        )
