"""Twilio voice callbacks. Each one is signed with the workspace's Twilio token."""

from __future__ import annotations

import uuid

from django.conf import settings

from core import tenancy
from core.errors import NotFoundError
from crm.consent.models import Channel
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.sms import services as sms
from crm.voice import services, twiml
from crm.voice.models import Call, CallDirection

PATH = "/api/v1/crm-public/voice/twilio"
KINDS = frozenset({"incoming", "dial-result", "status", "recording", "transcription", "connect"})
DEFAULT_GREETING_KEY = "voicemail_greeting"


def callback_url(public_id: str, kind: str) -> str:
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{PATH}/{public_id}/{kind}"


def receive(public_id: str, kind: str, signature: str, params: dict[str, str]) -> str:
    """Verify one callback and answer with the TwiML (or an empty document)."""
    if kind not in KINDS:
        raise NotFoundError("not found")
    credential = sms.credential_for(public_id)
    sms.verify_signature(credential, callback_url(public_id, kind), signature, params)
    workspace_id = credential.workspace_id
    with tenancy.use_workspace(workspace_id):
        handler = HANDLERS[kind]
        return handler(workspace_id, public_id, params)


def _call(workspace_id: uuid.UUID, params: dict[str, str]) -> Call:
    call = services.call_by_provider_id(workspace_id, params.get("CallSid", ""))
    if call is None:
        raise NotFoundError("call not found")
    return call


def _seconds(params: dict[str, str], key: str) -> int:
    value = params.get(key, "0")
    return int(value) if value.isdigit() else 0


def _incoming(workspace_id: uuid.UUID, public_id: str, params: dict[str, str]) -> str:
    to_number = params.get("To", "")
    call = services.record_incoming(
        workspace_id,
        provider_call_id=params["CallSid"],
        from_number=params.get("From", ""),
        to_number=to_number,
    )
    line = services.find_line(workspace_id, to_number)
    forward_to = str(line.config.get("forward_to", "")) if line else ""
    if not forward_to:
        return _voicemail(line, public_id, call)
    return twiml.dial(
        forward_to,
        timeout=services.ring_seconds(),
        action=callback_url(public_id, "dial-result"),
        record=True,
    )


def _voicemail(line: object, public_id: str, call: Call) -> str:
    config = getattr(line, "config", {}) or {}
    return twiml.voicemail(
        str(config.get(DEFAULT_GREETING_KEY, "")),
        max_seconds=services.voicemail_max_seconds(),
        action=callback_url(public_id, "recording"),
        transcribe_callback=callback_url(public_id, "transcription"),
    )


def _dial_result(workspace_id: uuid.UUID, public_id: str, params: dict[str, str]) -> str:
    call = _call(workspace_id, params)
    status = params.get("DialCallStatus", "")
    services.finish_dial(workspace_id, call, status, _seconds(params, "DialCallDuration"))
    if status == "completed":
        return twiml.empty()
    line = services.find_line(workspace_id, call.to_number)
    services.text_back(workspace_id, call, line)
    return _voicemail(line, public_id, call)


def _status(workspace_id: uuid.UUID, public_id: str, params: dict[str, str]) -> str:
    call = services.call_by_provider_id(workspace_id, params.get("CallSid", ""))
    if call is not None:
        services.apply_status(
            workspace_id, call, params.get("CallStatus", ""), _seconds(params, "CallDuration")
        )
    return twiml.empty()


def _recording(workspace_id: uuid.UUID, public_id: str, params: dict[str, str]) -> str:
    call = _call(workspace_id, params)
    services.attach_recording(
        workspace_id,
        call,
        params.get("RecordingUrl", ""),
        _seconds(params, "RecordingDuration"),
    )
    return twiml.empty()


def _transcription(workspace_id: uuid.UUID, public_id: str, params: dict[str, str]) -> str:
    call = _call(workspace_id, params)
    services.attach_transcript(call, params.get("TranscriptionText", ""))
    return twiml.empty()


def _connect(workspace_id: uuid.UUID, public_id: str, params: dict[str, str]) -> str:
    """The agent picked up a click-to-call: connect them to the customer."""
    contact_id = inbox.contact_of_outbound(workspace_id, Channel.VOICE, params.get("CallSid", ""))
    if contact_id is None:
        raise NotFoundError("call not found")
    contact = contacts.get_contact(workspace_id, contact_id)
    services.record_call(
        workspace_id,
        provider_call_id=params["CallSid"],
        direction=CallDirection.OUTBOUND,
        from_number=params.get("To", ""),
        to_number=contact.phone,
        contact_id=contact.pk,
    )
    return twiml.bridge(contact.phone)


HANDLERS = {
    "incoming": _incoming,
    "dial-result": _dial_result,
    "status": _status,
    "recording": _recording,
    "transcription": _transcription,
    "connect": _connect,
}
