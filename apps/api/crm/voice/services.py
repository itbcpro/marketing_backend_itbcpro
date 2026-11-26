"""Call logging, voicemail and missed-call text-back (public surface of the module)."""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import QuerySet

from audit.actors import Actor
from core.errors import NotFoundError, ValidationFailedError
from crm.consent.models import Channel, Purpose
from crm.contacts import services as contacts
from crm.events.publisher import publish
from crm.inbox import services as inbox
from crm.inbox.models import ChannelAccount
from crm.sending import services as sending
from crm.voice import events
from crm.voice.models import Call, CallDirection, CallStatus
from workspaces.models import Membership
from workspaces.scope import restrict

SYSTEM = Actor.system("voice")
PROVIDER = "twilio"
LINE_KEYS = ("forward_to", "voicemail_greeting", "text_back_body")
GREETING_LIMIT = 300
TEXT_BACK_LIMIT = 320
MISSED_DIAL_STATUSES = frozenset({"no-answer", "busy", "failed", "canceled"})


# ---- lines (the numbers that receive calls) ------------------------------------------------


def list_lines(workspace_id: uuid.UUID) -> list[ChannelAccount]:
    return inbox.list_accounts(workspace_id, Channel.VOICE)


def find_line(workspace_id: uuid.UUID, number: str) -> ChannelAccount | None:
    return inbox.find_account(workspace_id, Channel.VOICE, number)


def add_line(workspace_id: uuid.UUID, *, number: str, label: str = "") -> ChannelAccount:
    return inbox.add_account(
        workspace_id,
        channel=Channel.VOICE,
        provider=PROVIDER,
        external_id=number,
        display_name=label,
        config={},
    )


def configure_line(
    workspace_id: uuid.UUID, line_id: uuid.UUID, changes: dict[str, str]
) -> ChannelAccount:
    unknown = set(changes) - set(LINE_KEYS)
    if unknown:
        raise ValidationFailedError("invalid_setting")
    if len(changes.get("voicemail_greeting", "")) > GREETING_LIMIT:
        raise ValidationFailedError("greeting_too_long")
    if len(changes.get("text_back_body", "")) > TEXT_BACK_LIMIT:
        raise ValidationFailedError("text_back_too_long")
    return inbox.update_account_config(
        workspace_id, line_id, {k: v.strip() for k, v in changes.items()}
    )


def remove_line(workspace_id: uuid.UUID, line_id: uuid.UUID) -> None:
    inbox.remove_account(workspace_id, line_id)


# ---- the call log -----------------------------------------------------------------------------


def list_calls(
    workspace_id: uuid.UUID,
    membership: Membership | None = None,
    *,
    contact_id: uuid.UUID | None = None,
    limit: int = 50,
) -> QuerySet[Call]:
    """Calls the member may see: workspace scoped, and only their own contacts' when so limited."""
    rows: QuerySet[Call] = Call.objects.for_workspace(workspace_id).select_related("contact")
    if membership is not None:
        rows = restrict(rows, membership, owner_field="contact__assigned_to_id")
    if contact_id:
        rows = rows.filter(contact_id=contact_id)
    return rows[: max(1, min(limit, 200))]


def get_call(workspace_id: uuid.UUID, call_id: uuid.UUID) -> Call:
    found = Call.objects.for_workspace(workspace_id).filter(pk=call_id).first()
    if found is None:
        raise NotFoundError("call not found")
    return found


def call_by_provider_id(workspace_id: uuid.UUID, provider_call_id: str) -> Call | None:
    return (
        Call.objects.for_workspace(workspace_id).filter(provider_call_id=provider_call_id).first()
    )


def _ref(call: Call) -> dict[str, Any]:
    return {"call_id": call.pk, "contact_id": call.contact_id, "direction": call.direction}


def record_call(
    workspace_id: uuid.UUID,
    *,
    provider_call_id: str,
    direction: str,
    from_number: str,
    to_number: str,
    contact_id: uuid.UUID,
) -> Call:
    """Log a call once; a repeated callback returns the same row."""
    try:
        with transaction.atomic():
            return Call.objects.create(
                workspace_id=workspace_id,
                contact_id=contact_id,
                direction=direction,
                provider_call_id=provider_call_id,
                from_number=from_number,
                to_number=to_number,
            )
    except IntegrityError:
        return Call.objects.for_workspace(workspace_id).get(provider_call_id=provider_call_id)


def record_incoming(
    workspace_id: uuid.UUID, *, provider_call_id: str, from_number: str, to_number: str
) -> Call:
    contact = inbox.resolve_contact(
        workspace_id, channel=Channel.VOICE, sender_channel="phone", value=from_number
    )
    return record_call(
        workspace_id,
        provider_call_id=provider_call_id,
        direction=CallDirection.INBOUND,
        from_number=from_number,
        to_number=to_number,
        contact_id=contact.pk,
    )


def finish_dial(workspace_id: uuid.UUID, call: Call, dial_status: str, seconds: int) -> Call:
    """The first leg of an inbound call ended: answered, or nobody picked up."""
    if call.status != CallStatus.RINGING:
        return call
    missed = dial_status in MISSED_DIAL_STATUSES
    with transaction.atomic():
        call.status = CallStatus.MISSED if missed else CallStatus.COMPLETED
        call.duration_seconds = 0 if missed else seconds
        call.save(update_fields=["status", "duration_seconds", "updated_at"])
        if missed:
            publish(
                "crm.call.missed",
                workspace_id=workspace_id,
                actor=SYSTEM,
                data=events.CallMissed(**_ref(call)),
            )
        else:
            publish(
                "crm.call.completed",
                workspace_id=workspace_id,
                actor=SYSTEM,
                data=events.CallCompleted(**_ref(call), duration_seconds=seconds),
            )
    return call


def apply_status(workspace_id: uuid.UUID, call: Call, status: str, seconds: int) -> Call:
    """Twilio's final report for the whole call. It never overrides a missed call."""
    if status == "completed" and call.status == CallStatus.RINGING:
        return finish_dial(workspace_id, call, "completed", seconds)
    if status in MISSED_DIAL_STATUSES and call.status == CallStatus.RINGING:
        return finish_dial(workspace_id, call, status, 0)
    if status == "completed" and call.status == CallStatus.COMPLETED and seconds:
        call.duration_seconds = seconds
        call.save(update_fields=["duration_seconds", "updated_at"])
    return call


def attach_recording(workspace_id: uuid.UUID, call: Call, url: str, seconds: int) -> Call:
    with transaction.atomic():
        call.recording_url = url[:500]
        call.recording_seconds = seconds
        call.voicemail = call.status == CallStatus.MISSED
        call.save(update_fields=["recording_url", "recording_seconds", "voicemail", "updated_at"])
        if call.voicemail:
            publish(
                "crm.call.voicemail",
                workspace_id=workspace_id,
                actor=SYSTEM,
                data=events.CallVoicemail(**_ref(call), recording_seconds=seconds),
            )
    return call


def attach_transcript(call: Call, text: str) -> Call:
    call.transcript = text
    call.save(update_fields=["transcript", "updated_at"])
    return call


def text_back(workspace_id: uuid.UUID, call: Call, line: ChannelAccount | None) -> bool:
    """Reply to a missed caller by text, once. Goes through the gateway like any message."""
    body = str(line.config.get("text_back_body", "")) if line else ""
    if not body or call.text_back_sent or call.direction != CallDirection.INBOUND:
        return False
    contacts.get_contact(workspace_id, call.contact_id)
    # Reply from the number the caller dialled, when it is also set up for texting.
    same_number = inbox.find_account(workspace_id, Channel.SMS, line.external_id) if line else None
    outcome = sending.send(
        workspace_id=workspace_id,
        actor=SYSTEM,
        contact_id=call.contact_id,
        channel=Channel.SMS,
        body=body,
        purpose=Purpose.TRANSACTIONAL,
        channel_account_id=same_number.pk if same_number else None,
        idempotency_key=f"missed-call-text-back:{call.pk}",
    )
    call.text_back_sent = True
    call.save(update_fields=["text_back_sent", "updated_at"])
    return not outcome.blocked


def ring_seconds() -> int:
    return int(settings.CRM_VOICE_RING_SECONDS)


def voicemail_max_seconds() -> int:
    return int(settings.CRM_VOICEMAIL_MAX_SECONDS)
