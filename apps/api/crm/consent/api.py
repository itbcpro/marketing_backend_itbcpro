"""Consent: current state, history, manual changes, evidence export and legal profile."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.errors import ValidationFailedError
from crm.consent import services
from crm.consent.models import Source
from crm.consent.profiles import PROFILES
from crm.contacts import services as contacts
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-consent"], auth=api_auth)

# Sources a person or integration may claim through the API; the rest are written by the system.
API_SOURCES = frozenset({Source.FORM, Source.MANUAL, Source.API, Source.IMPORT, Source.LINK})


class StateOut(Schema):
    channel: str
    purpose: str
    status: str
    expires_at: datetime | None
    changed_at: datetime


class EventOut(Schema):
    id: uuid.UUID
    channel: str
    purpose: str
    status: str
    source: str
    evidence: dict[str, Any]
    actor_type: str
    actor_id: str
    occurred_at: datetime
    expires_at: datetime | None


class ConsentIn(Schema):
    channel: str
    purpose: str
    status: str
    source: str = Source.MANUAL
    evidence: dict[str, Any] = {}
    expires_at: datetime | None = None


class CheckOut(Schema):
    allowed: bool
    reason: str


class ConsentSettingsOut(Schema):
    law_profile: str
    quiet_hours_enabled: bool
    available_profiles: dict[str, str]


class ConsentSettingsIn(Schema):
    law_profile: str
    quiet_hours_enabled: bool | None = None


@router.get(
    "/contacts/{contact_id}/consent", response=list[StateOut], operation_id="crm_consent_states"
)
def states(request: HttpRequest, contact_id: uuid.UUID) -> list[StateOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    contacts.get_visible_contact(membership, contact_id)
    rows = services.states_of(membership.workspace_id, contact_id)
    return [StateOut.model_validate(r, from_attributes=True) for r in rows]


@router.get(
    "/contacts/{contact_id}/consent/history",
    response=list[EventOut],
    operation_id="crm_consent_history",
)
def history(
    request: HttpRequest, contact_id: uuid.UUID, channel: str | None = None
) -> list[EventOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    contacts.get_visible_contact(membership, contact_id)
    rows = services.history(membership.workspace_id, contact_id, channel)
    return [EventOut.model_validate(r, from_attributes=True) for r in rows]


@router.post(
    "/contacts/{contact_id}/consent", response={201: EventOut}, operation_id="crm_consent_record"
)
def record(request: HttpRequest, contact_id: uuid.UUID, payload: ConsentIn) -> Status[EventOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    contacts.get_visible_contact(membership, contact_id)
    if payload.source not in API_SOURCES:
        raise ValidationFailedError("invalid source", errors={"source": "invalid_value"})
    entry = services.record(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        contact_id=contact_id,
        **payload.model_dump(),
    )
    return Status(201, EventOut.model_validate(entry, from_attributes=True))


@router.get(
    "/contacts/{contact_id}/consent/check", response=CheckOut, operation_id="crm_consent_check"
)
def check(request: HttpRequest, contact_id: uuid.UUID, channel: str, purpose: str) -> CheckOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    contacts.get_visible_contact(membership, contact_id)
    decision = services.decide(
        workspace_id=membership.workspace_id,
        contact_id=contact_id,
        channel=channel,
        purpose=purpose,
    )
    return CheckOut(allowed=decision.allowed, reason=decision.reason)


@router.get(
    "/contacts/{contact_id}/consent/export",
    response=list[dict[str, Any]],
    operation_id="crm_consent_export",
)
def export(request: HttpRequest, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    membership = resolve_workspace(request, Permission.CRM_IMPORT_EXPORT)
    contacts.get_visible_contact(membership, contact_id)
    return services.evidence_export(membership.workspace_id, contact_id)


def _settings_out(row: Any) -> ConsentSettingsOut:
    return ConsentSettingsOut(
        law_profile=row.law_profile,
        quiet_hours_enabled=row.quiet_hours_enabled,
        available_profiles={k: p.label for k, p in PROFILES.items()},
    )


@router.get(
    "/consent/settings", response=ConsentSettingsOut, operation_id="crm_consent_settings_get"
)
def get_settings(request: HttpRequest) -> ConsentSettingsOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return _settings_out(services.get_settings(membership.workspace_id))


@router.put(
    "/consent/settings", response=ConsentSettingsOut, operation_id="crm_consent_settings_put"
)
def put_settings(request: HttpRequest, payload: ConsentSettingsIn) -> ConsentSettingsOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    if payload.law_profile not in PROFILES:
        raise ValidationFailedError("invalid profile", errors={"law_profile": "invalid_value"})
    row = services.set_law_profile(
        workspace_id=membership.workspace_id,
        profile=payload.law_profile,
        quiet_hours=payload.quiet_hours_enabled,
    )
    return _settings_out(row)
