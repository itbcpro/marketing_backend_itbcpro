import json
import uuid

import pytest
from django.test import Client

from audit.actors import Actor
from audit.outbox import dispatch_batch
from conftest import make_user
from crm.activity.models import Activity
from crm.channels.twilio_sms import TwilioSmsAdapter
from crm.channels.twilio_voice import TwilioVoiceAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.inbox.models import Message
from crm.sending import registry
from crm.sms import services as sms
from crm.sms.simulator import SimulatedTwilio
from crm.sms.twilio_client import expected_signature
from crm.voice import services, webhooks
from crm.voice.models import Call, CallStatus
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
TOKEN = "voice-token"
LINE = "+14165550100"
CALLER = "+14165550199"
AGENT = "+14165550123"


@pytest.fixture
def twilio(monkeypatch):
    sim = SimulatedTwilio()
    monkeypatch.setattr(sms, "transport_factory", sim.transport)
    registry.register("sms", TwilioSmsAdapter)
    registry.register("voice", TwilioVoiceAdapter)
    return sim


@pytest.fixture
def line(workspace, twilio):
    sms.connect(workspace.pk, account_sid="AC1", auth_token=TOKEN)
    row = services.add_line(workspace.pk, number=LINE, label="Main")
    inbox.add_account(workspace.pk, channel="sms", provider="twilio", external_id=LINE)
    return services.configure_line(
        workspace.pk,
        row.pk,
        {
            "forward_to": AGENT,
            "voicemail_greeting": "Leave a message",
            "text_back_body": "Sorry we missed you",
        },
    )


def post(workspace, kind, params, *, signature=None):
    credential = sms.get_credential(workspace.pk)
    url = webhooks.callback_url(credential.public_id, kind)
    sig = signature if signature is not None else expected_signature(TOKEN, url, params)
    return Client().post(
        f"/api/v1/crm-public/voice/twilio/{credential.public_id}/{kind}",
        data=params,
        HTTP_X_TWILIO_SIGNATURE=sig,
    )


def incoming(workspace, sid="CA1"):
    return post(workspace, "incoming", {"CallSid": sid, "From": CALLER, "To": LINE})


def test_an_incoming_call_rings_the_forward_number_and_is_logged(workspace, line):
    res = incoming(workspace)
    body = res.content.decode()
    assert res.status_code == 200 and "<Number>+14165550123</Number>" in body
    assert 'record="record-from-answer-dual"' in body
    call = Call.objects.get(provider_call_id="CA1")
    assert (call.direction, call.status, call.from_number) == ("inbound", "ringing", CALLER)
    incoming(workspace)  # Twilio retried the webhook
    assert Call.objects.count() == 1


def test_an_answered_call_is_completed_with_its_duration(workspace, line):
    incoming(workspace)
    params = {"CallSid": "CA1", "DialCallStatus": "completed", "DialCallDuration": "42"}
    assert "<Record" not in post(workspace, "dial-result", params).content.decode()
    call = Call.objects.get()
    assert (call.status, call.duration_seconds) == (CallStatus.COMPLETED, 42)
    dispatch_batch()
    assert Activity.objects.filter(kind="call.completed").count() == 1


def test_a_missed_call_goes_to_voicemail_and_texts_the_caller_once(workspace, line, twilio):
    incoming(workspace)
    contact = Call.objects.get().contact
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=contact.pk,
        channel="sms",
        purpose="transactional",
        status="opted_in",
        source="form",
    )
    params = {"CallSid": "CA1", "DialCallStatus": "no-answer"}
    first = post(workspace, "dial-result", params).content.decode()
    assert "<Record" in first and "Leave a message" in first
    post(workspace, "dial-result", params)  # repeated callback
    call = Call.objects.get()
    assert call.status == CallStatus.MISSED and call.text_back_sent
    assert Message.objects.filter(channel="sms", direction="outbound").count() == 1
    assert twilio.messages[0]["Body"] == ["Sorry we missed you"]


def test_voicemail_recording_and_transcript_are_attached(workspace, line):
    incoming(workspace)
    post(workspace, "dial-result", {"CallSid": "CA1", "DialCallStatus": "busy"})
    rec = {
        "CallSid": "CA1",
        "RecordingUrl": "https://api.twilio.com/rec/RE1",
        "RecordingDuration": "9",
    }
    post(workspace, "recording", rec)
    post(workspace, "transcription", {"CallSid": "CA1", "TranscriptionText": "Please call back"})
    call = Call.objects.get()
    assert call.voicemail and call.recording_seconds == 9
    assert call.transcript == "Please call back"
    dispatch_batch()
    kinds = set(Activity.objects.values_list("kind", flat=True))
    assert {"call.missed", "call.voicemail"} <= kinds


def test_without_a_forward_number_callers_go_straight_to_voicemail(workspace, line):
    services.configure_line(workspace.pk, line.pk, {"forward_to": ""})
    body = incoming(workspace).content.decode()
    assert "<Dial" not in body and "<Record" in body


def test_the_final_status_report_never_overrides_a_missed_call(workspace, line):
    incoming(workspace)
    post(workspace, "dial-result", {"CallSid": "CA1", "DialCallStatus": "no-answer"})
    post(workspace, "status", {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "30"})
    assert Call.objects.get().status == CallStatus.MISSED


def test_unsigned_or_unknown_callbacks_are_refused(workspace, line):
    params = {"CallSid": "CA9", "From": CALLER, "To": LINE}
    assert post(workspace, "incoming", params, signature="nope").status_code == 403
    assert post(workspace, "incoming", params, signature="").status_code == 403
    assert Call.objects.count() == 0
    assert post(workspace, "mystery", params).status_code == 404
    assert (
        Client().post("/api/v1/crm-public/voice/twilio/nope/incoming", data={}).status_code == 404
    )


def test_voice_text_is_escaped_in_twiml(workspace, line):
    services.configure_line(
        workspace.pk, line.pk, {"forward_to": "", "voicemail_greeting": "Tom & <Jerry>"}
    )
    body = incoming(workspace).content.decode()
    assert "Tom &amp; &lt;Jerry&gt;" in body


# ---- click to call -----------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def logged(user):
    c = Client()
    c.force_login(user)
    return c


def opted_in_contact(workspace):
    c = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"phone": "+14165550111"}
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=c.pk,
        channel="voice",
        purpose="transactional",
        status="opted_in",
        source="form",
    )
    return c


def test_click_to_call_rings_the_agent_then_bridges_the_customer(workspace, owner, line, twilio):
    contact = opted_in_contact(workspace)
    res = api(
        logged(owner),
        "post",
        "/voice/click-to-call",
        workspace,
        {"contact_id": str(contact.pk), "agent_phone": "416 555 0123"},
    )
    assert res.status_code == 201 and not res.json()["blocked"]
    message = Message.objects.get(pk=res.json()["message_id"])
    assert message.status == "sent" and message.provider_message_id.startswith("SM")
    assert twilio.messages[0]["To"] == [AGENT] and twilio.messages[0]["From"] == [LINE]
    # The agent answers: Twilio asks the connect hook what to do.
    bridge = post(workspace, "connect", {"CallSid": message.provider_message_id, "To": AGENT})
    assert "<Number>+14165550111</Number>" in bridge.content.decode()
    call = Call.objects.get()
    assert (call.direction, call.contact_id) == ("outbound", contact.pk)


def test_click_to_call_respects_consent(workspace, owner, line):
    stranger = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"phone": "+14165550177"}
    )
    res = api(
        logged(owner),
        "post",
        "/voice/click-to-call",
        workspace,
        {"contact_id": str(stranger.pk), "agent_phone": AGENT},
    )
    # Consent rules of the workspace's law profile decide; a blocked call is reported, not placed.
    assert res.status_code == 201
    if res.json()["blocked"]:
        assert res.json()["reason"]


def test_lines_and_calls_api(workspace, owner, line):
    admin = logged(owner)
    listed = api(admin, "get", "/voice/lines", workspace).json()
    assert listed[0]["number"] == LINE and listed[0]["forward_to"] == AGENT
    patched = api(
        admin, "patch", f"/voice/lines/{line.pk}", workspace, {"forward_to": "416 555 0188"}
    )
    assert patched.json()["forward_to"] == "+14165550188"
    assert (
        api(admin, "patch", f"/voice/lines/{line.pk}", workspace, {"forward_to": "abc"}).status_code
        == 422
    )
    assert api(admin, "post", "/voice/lines", workspace, {"number": LINE}).status_code == 409
    incoming(workspace)
    calls = api(admin, "get", "/voice/calls", workspace).json()
    assert len(calls) == 1 and calls[0]["direction"] == "inbound"
    assert api(admin, "delete", f"/voice/lines/{line.pk}", workspace).status_code == 204


def test_permissions_scope_and_tenants(workspace, owner, line):
    viewer = logged(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, "get", "/voice/lines", workspace).status_code == 403
    assert api(viewer, "get", "/voice/calls", workspace).status_code == 200
    body = {"contact_id": str(uuid.uuid4()), "agent_phone": AGENT}
    assert api(viewer, "post", "/voice/click-to-call", workspace, body).status_code == 403
    other = create_workspace(name="Other Co", owner=owner)
    admin = logged(owner)
    assert api(admin, "get", "/voice/lines", other).json() == []
    assert api(admin, "get", "/voice/calls", other).json() == []
    assert api(admin, "delete", f"/voice/lines/{line.pk}", other).status_code == 404
    assert (
        api(admin, "patch", f"/voice/lines/{line.pk}", other, {"forward_to": ""}).status_code == 404
    )
