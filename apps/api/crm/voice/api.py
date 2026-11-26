"""Voice lines, the call log and click-to-call."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from django.utils.translation import gettext as _
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.errors import ValidationFailedError
from crm.common.normalize import InvalidValueError, normalise_phone
from crm.consent.models import Channel
from crm.contacts import services as contacts
from crm.inbox.models import ChannelAccount
from crm.sending import services as sending
from crm.voice import services
from crm.voice.models import Call
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-voice"], auth=api_auth)
AGENT_KEY = "agent_phone"


class LineIn(Schema):
    number: str
    label: str = ""


class LinePatch(Schema):
    forward_to: str | None = None
    voicemail_greeting: str | None = None
    text_back_body: str | None = None


class LineOut(Schema):
    id: uuid.UUID
    number: str
    label: str
    forward_to: str
    voicemail_greeting: str
    text_back_body: str


class CallOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str
    direction: str
    from_number: str
    to_number: str
    status: str
    duration_seconds: int
    voicemail: bool
    recording_url: str
    transcript: str
    created_at: datetime


class ClickToCallIn(Schema):
    contact_id: uuid.UUID
    agent_phone: str


class ClickToCallOut(Schema):
    message_id: uuid.UUID
    blocked: bool
    reason: str


def _phone(value: str) -> str:
    try:
        return normalise_phone(value)
    except InvalidValueError as exc:
        raise ValidationFailedError("invalid number", errors={"number": str(exc)}) from exc


def _line(row: ChannelAccount) -> LineOut:
    cfg = row.config
    return LineOut(
        id=row.pk,
        number=row.external_id,
        label=row.display_name,
        forward_to=str(cfg.get("forward_to", "")),
        voicemail_greeting=str(cfg.get("voicemail_greeting", "")),
        text_back_body=str(cfg.get("text_back_body", "")),
    )


def _call(row: Call) -> CallOut:
    return CallOut(
        id=row.pk,
        contact_id=row.contact_id,
        contact_name=row.contact.full_name or row.contact.phone or row.contact.email,
        direction=row.direction,
        from_number=row.from_number,
        to_number=row.to_number,
        status=row.status,
        duration_seconds=row.duration_seconds,
        voicemail=row.voicemail,
        recording_url=row.recording_url,
        transcript=row.transcript,
        created_at=row.created_at,
    )


@router.get("/voice/lines", response=list[LineOut], operation_id="crm_voice_lines")
def lines(request: HttpRequest) -> list[LineOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return [_line(r) for r in services.list_lines(m.workspace_id)]


@router.post("/voice/lines", response={201: LineOut}, operation_id="crm_voice_line_add")
def add_line(request: HttpRequest, payload: LineIn) -> Status[LineOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    row = services.add_line(m.workspace_id, number=_phone(payload.number), label=payload.label)
    return Status(201, _line(row))


@router.patch("/voice/lines/{line_id}", response=LineOut, operation_id="crm_voice_line_update")
def update_line(request: HttpRequest, line_id: uuid.UUID, payload: LinePatch) -> LineOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if changes.get("forward_to"):
        changes["forward_to"] = _phone(changes["forward_to"])
    return _line(services.configure_line(m.workspace_id, line_id, changes))


@router.delete("/voice/lines/{line_id}", response={204: None}, operation_id="crm_voice_line_remove")
def remove_line(request: HttpRequest, line_id: uuid.UUID) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    services.remove_line(m.workspace_id, line_id)
    return Status(204, None)


@router.get("/voice/calls", response=list[CallOut], operation_id="crm_voice_calls")
def calls(
    request: HttpRequest, contact_id: uuid.UUID | None = None, limit: int = 50
) -> list[CallOut]:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    if contact_id:
        contacts.get_visible_contact(m, contact_id)
    rows = services.list_calls(m.workspace_id, m, contact_id=contact_id, limit=limit)
    return [_call(r) for r in rows]


@router.post("/voice/click-to-call", response={201: ClickToCallOut}, operation_id="crm_voice_call")
def click_to_call(request: HttpRequest, payload: ClickToCallIn) -> Status[ClickToCallOut]:
    """Ring the agent's phone first; when they answer, the customer is dialled."""
    m = resolve_workspace(request, Permission.CRM_SEND)
    contacts.get_visible_contact(m, payload.contact_id)
    outcome = sending.send(
        workspace_id=m.workspace_id,
        actor=Actor.human(m.user_id),
        contact_id=payload.contact_id,
        channel=Channel.VOICE,
        body=_("Call started"),
        attachments=[{AGENT_KEY: _phone(payload.agent_phone)}],
    )
    return Status(
        201,
        ClickToCallOut(
            message_id=outcome.message.pk, blocked=outcome.blocked, reason=outcome.reason
        ),
    )
