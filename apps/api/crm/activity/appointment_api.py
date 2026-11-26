"""Appointment endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.api.pagination import DEFAULT_LIMIT, MAX_LIMIT
from core.errors import NotFoundError
from crm.activity import appointments
from crm.activity.models import Appointment
from crm.contacts import services as contacts
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.models import Membership
from workspaces.permissions import Permission

router = Router(tags=["crm-appointments"], auth=api_auth)


class AppointmentIn(Schema):
    title: str
    starts_at: datetime
    ends_at: datetime
    location: str = ""
    notes: str = ""
    assigned_to: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None


class AppointmentPatch(Schema):
    title: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    location: str | None = None
    notes: str | None = None
    assigned_to: uuid.UUID | None = None


class AppointmentStatusIn(Schema):
    status: str


class AppointmentOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str
    opportunity_id: uuid.UUID | None
    title: str
    starts_at: datetime
    ends_at: datetime
    location: str
    notes: str
    assigned_to_id: uuid.UUID | None
    status: str
    created_at: datetime


def _actor(m: Membership) -> Actor:
    return Actor.human(m.user_id)


def _out(row: Appointment) -> AppointmentOut:
    return AppointmentOut(
        id=row.pk,
        contact_id=row.contact_id,
        contact_name=str(row.contact),
        opportunity_id=row.opportunity_id,
        title=row.title,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        location=row.location,
        notes=row.notes,
        assigned_to_id=row.assigned_to_id,
        status=row.status,
        created_at=row.created_at,
    )


def _visible(membership: Membership, appointment_id: uuid.UUID) -> Appointment:
    found = appointments.visible(membership).filter(pk=appointment_id).first()
    if found is None:
        raise NotFoundError("appointment not found")
    return found


@router.get("/appointments", response=list[AppointmentOut], operation_id="crm_appointments_list")
def list_appointments(
    request: HttpRequest,
    scope: str = "upcoming",
    contact_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[AppointmentOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = appointments.listing(
        membership, scope=scope, contact_id=contact_id, opportunity_id=opportunity_id
    )
    return [_out(r) for r in rows[: max(1, min(limit, MAX_LIMIT))]]


@router.post(
    "/contacts/{contact_id}/appointments",
    response={201: AppointmentOut},
    operation_id="crm_appointments_create",
)
def create_appointment(
    request: HttpRequest, contact_id: uuid.UUID, payload: AppointmentIn
) -> Status[AppointmentOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    contacts.get_visible_contact(membership, contact_id)
    row = appointments.book(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        contact_id=contact_id,
        **payload.model_dump(),
    )
    return Status(201, _out(row))


@router.patch(
    "/appointments/{appointment_id}",
    response=AppointmentOut,
    operation_id="crm_appointments_update",
)
def update_appointment(
    request: HttpRequest, appointment_id: uuid.UUID, payload: AppointmentPatch
) -> AppointmentOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible(membership, appointment_id)
    row = appointments.update(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        appointment_id=appointment_id,
        data=payload.model_dump(exclude_unset=True),
    )
    return _out(row)


@router.post(
    "/appointments/{appointment_id}/status",
    response=AppointmentOut,
    operation_id="crm_appointments_status",
)
def set_appointment_status(
    request: HttpRequest, appointment_id: uuid.UUID, payload: AppointmentStatusIn
) -> AppointmentOut:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible(membership, appointment_id)
    row = appointments.set_status(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        appointment_id=appointment_id,
        status=payload.status,
    )
    return _out(row)


@router.delete(
    "/appointments/{appointment_id}", response={204: None}, operation_id="crm_appointments_delete"
)
def delete_appointment(request: HttpRequest, appointment_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    _visible(membership, appointment_id)
    appointments.delete(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        appointment_id=appointment_id,
    )
    return Status(204, None)
