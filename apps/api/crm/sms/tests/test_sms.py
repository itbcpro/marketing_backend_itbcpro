import json
import uuid

import pytest
from django.test import Client

from audit.actors import Actor
from conftest import make_user
from core.errors import NotFoundError, ValidationFailedError
from crm.channels.twilio_sms import TwilioSmsAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.inbox.models import Message
from crm.sending import gateway, registry
from crm.sending.ports import OutboundRequest, RetryableAdapterError
from crm.sms import errors, services
from crm.sms.simulator import SimulatedTwilio
from crm.sms.twilio_client import expected_signature
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
NUMBER = "+14165550100"
TOKEN = "secret-token"


@pytest.fixture
def twilio(monkeypatch):
    sim = SimulatedTwilio()
    monkeypatch.setattr(services, "transport_factory", sim.transport)
    registry.register("sms", TwilioSmsAdapter)  # other tests may have swapped in a fake
    return sim


@pytest.fixture
def connected(workspace, twilio):
    services.connect(workspace.pk, account_sid="AC123", auth_token=TOKEN)
    inbox.add_account(
        workspace.pk, channel="sms", provider="twilio", external_id=NUMBER, display_name="Main"
    )


@pytest.fixture
def person(workspace):
    c = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"phone": "+1 416 555 0111"}
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=c.pk,
        channel="sms",
        purpose="marketing",
        status="opted_in",
        source="form",
    )
    return c


def send(workspace, person, body="Hello there"):
    return gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel="sms",
        body=body,
    )


def test_credentials_are_sealed_and_replaceable(workspace):
    row = services.connect(workspace.pk, account_sid="AC1", auth_token="tok-1")
    assert b"tok-1" not in bytes(row.token_ciphertext)
    assert services.auth_token_of(row) == "tok-1"
    again = services.connect(workspace.pk, account_sid="AC2", auth_token="tok-2")
    assert again.pk == row.pk and again.public_id == row.public_id
    assert services.auth_token_of(again) == "tok-2"
    with pytest.raises(ValidationFailedError):
        services.connect(workspace.pk, account_sid="", auth_token="x")
    services.disconnect(workspace.pk)
    with pytest.raises(NotFoundError):
        services.get_credential(workspace.pk)


def test_a_text_goes_out_from_the_default_number_with_a_status_callback(
    workspace, person, connected, twilio
):
    outcome = send(workspace, person)
    message = Message.objects.get(pk=outcome.message.pk)
    assert message.status == "sent" and message.provider_message_id.startswith("SM")
    sent = twilio.messages[0]
    assert sent["From"] == [NUMBER] and sent["To"] == ["+14165550111"]
    assert sent["Body"] == ["Hello there"]
    assert sent["StatusCallback"][0].endswith("/status")


def test_retries_reuse_the_idempotency_token(workspace, connected, twilio):
    request = OutboundRequest(
        workspace_id=workspace.pk,
        message_id=uuid.uuid4(),
        channel="sms",
        to="+14165550111",
        body="Hi",
    )
    adapter = TwilioSmsAdapter()
    twilio.fail_next(503)
    with pytest.raises(RetryableAdapterError):
        adapter.send(request)
    first = adapter.send(request)
    again = adapter.send(request)  # the job crashed after sending and runs again
    assert first.provider_message_id == again.provider_message_id
    assert len(twilio.messages) == 1


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, 21211, "sms_invalid_number"),
        (400, 21610, "sms_recipient_opted_out"),
        (401, 20003, errors.BAD_CREDENTIALS),
        (400, 99999, errors.UNKNOWN),
    ],
)
def test_provider_refusals_become_stable_codes(
    workspace, person, connected, twilio, status, code, expected
):
    twilio.fail_next(status, code)
    message = Message.objects.get(pk=send(workspace, person).message.pk)
    assert (message.status, message.error_code) == ("failed", expected)


def test_without_an_account_or_a_number_the_text_fails_clearly(workspace, person, twilio):
    message = Message.objects.get(pk=send(workspace, person).message.pk)
    assert message.error_code == errors.NO_CREDENTIALS
    services.connect(workspace.pk, account_sid="AC1", auth_token="t")
    other = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"phone": "+14165550122"}
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=other.pk,
        channel="sms",
        purpose="marketing",
        status="opted_in",
        source="form",
    )
    again = Message.objects.get(pk=send(workspace, other).message.pk)
    assert again.error_code == errors.NO_SENDER


def test_credentials_do_not_cross_workspaces(workspace, owner, connected):
    other = create_workspace(name="Other Co", owner=owner)
    with pytest.raises(NotFoundError):
        services.get_credential(other.pk)


def test_signature_matches_twilios_documented_scheme():
    # Example from Twilio's security documentation.
    token = "12345"
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+14158675310",
        "Digits": "1234",
        "From": "+14158675310",
        "To": "+18005551212",
    }
    assert expected_signature(token, url, params) == "GvWf1cFY/Q7PnoempGyD5oXAezc="


# ---- callbacks and API ------------------------------------------------------------------------


def signed_post(client, workspace, kind, params, *, token=TOKEN, signature=None):
    credential = services.get_credential(workspace.pk)
    url = services.callback_url(credential, kind)
    path = f"/api/v1/crm-public/sms/twilio/{credential.public_id}/{kind}"
    sig = signature if signature is not None else expected_signature(token, url, params)
    return client.post(path, data=params, HTTP_X_TWILIO_SIGNATURE=sig)


def test_a_signed_reply_creates_the_message_once(workspace, connected):
    client = Client()
    params = {"MessageSid": "SMin1", "From": "+14165550199", "To": NUMBER, "Body": "Yes please"}
    assert signed_post(client, workspace, "inbound", params).status_code == 200
    assert signed_post(client, workspace, "inbound", params).status_code == 200
    rows = Message.objects.filter(provider_message_id="SMin1")
    assert rows.count() == 1 and rows.get().body == "Yes please"
    assert rows.get().direction == "inbound"


def test_bad_signatures_are_refused(workspace, connected):
    params = {"MessageSid": "SMx", "From": "+14165550199", "To": NUMBER, "Body": "Hi"}
    for sig in ("", "nope", expected_signature("wrong-token", "https://x.test/", params)):
        res = signed_post(Client(), workspace, "inbound", params, signature=sig)
        assert res.status_code == 403
    assert Message.objects.filter(provider_message_id="SMx").count() == 0


def test_stop_by_text_opts_the_contact_out(workspace, person, connected):
    params = {"MessageSid": "SMstop", "From": "+14165550111", "To": NUMBER, "Body": "STOP"}
    assert signed_post(Client(), workspace, "inbound", params).status_code == 200
    assert send(workspace, person).blocked


def test_delivery_reports_update_the_message(workspace, person, connected, twilio):
    message = Message.objects.get(pk=send(workspace, person).message.pk)
    sid = message.provider_message_id
    client = Client()
    delivered = {"MessageSid": sid, "MessageStatus": "delivered"}
    assert signed_post(client, workspace, "status", delivered).status_code == 200
    message.refresh_from_db()
    assert message.status == "delivered"
    failed = {"MessageSid": sid, "MessageStatus": "undelivered", "ErrorCode": "30007"}
    signed_post(client, workspace, "status", failed)
    message.refresh_from_db()
    assert message.status == "delivered"  # a late failure report never moves it backwards
    ignored = {"MessageSid": sid, "MessageStatus": "queued"}
    assert signed_post(client, workspace, "status", ignored).status_code == 200


def test_an_unknown_endpoint_is_not_found(workspace, connected):
    res = Client().post("/api/v1/crm-public/sms/twilio/nope/inbound", data={})
    assert res.status_code == 404


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def logged(user):
    c = Client()
    c.force_login(user)
    return c


def test_management_api_roundtrip(workspace, owner):
    admin = logged(owner)
    assert api(admin, "get", "/sms/account", workspace).json() == {
        "connected": False,
        "account_sid": "",
        "status_callback_url": "",
        "inbound_url": "",
    }
    made = api(
        admin, "put", "/sms/account", workspace, {"account_sid": "AC9", "auth_token": "tok"}
    ).json()
    assert made["connected"] and made["inbound_url"].endswith("/inbound")
    assert "tok" not in json.dumps(made)
    added = api(
        admin, "post", "/sms/numbers", workspace, {"number": "416 555 0100", "label": "Main"}
    )
    assert added.status_code == 201 and added.json()["number"] == "+14165550100"
    nid = added.json()["id"]
    dup = api(admin, "post", "/sms/numbers", workspace, {"number": "+14165550100"})
    assert dup.status_code == 409
    assert api(admin, "post", "/sms/numbers", workspace, {"number": "abc"}).status_code == 422
    steps = api(admin, "get", "/sms/registration?country=CA", workspace).json()
    assert steps["steps"][0]["key"] == "business_profile"
    done = api(
        admin,
        "put",
        f"/sms/numbers/{nid}/registration",
        workspace,
        {"status": "pending", "completed_steps": ["business_profile"]},
    )
    assert done.json()["registration_status"] == "pending"
    bad = {"status": "pending", "completed_steps": ["made_up"]}
    assert api(admin, "put", f"/sms/numbers/{nid}/registration", workspace, bad).status_code == 422
    assert api(admin, "delete", f"/sms/numbers/{nid}", workspace).status_code == 204
    assert api(admin, "delete", "/sms/account", workspace).status_code == 204


def test_management_api_permissions_and_tenants(workspace, owner, connected):
    viewer = logged(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, "get", "/sms/numbers", workspace).status_code == 403
    assert (
        api(
            viewer, "put", "/sms/account", workspace, {"account_sid": "a", "auth_token": "b"}
        ).status_code
        == 403
    )
    other = create_workspace(name="Other Co", owner=owner)
    admin = logged(owner)
    assert api(admin, "get", "/sms/numbers", other).json() == []
    assert api(admin, "get", "/sms/account", other).json()["connected"] is False
    number = inbox.list_accounts(workspace.pk, "sms")[0]
    assert api(admin, "delete", f"/sms/numbers/{number.pk}", other).status_code == 404
    reg = {"status": "approved", "completed_steps": []}
    assert (
        api(admin, "put", f"/sms/numbers/{number.pk}/registration", other, reg).status_code == 404
    )
