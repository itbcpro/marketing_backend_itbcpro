import json
import time
import uuid
from email.message import EmailMessage

import pytest
from django.test import Client

from audit.actors import Actor
from conftest import make_user
from core.errors import NotFoundError, ValidationFailedError
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox.models import Message
from crm.sending import gateway, registry, suppression
from crm.webhooks import services
from crm.webhooks.models import DeliveryStatus, WebhookDelivery, WebhookKind
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def person(workspace):
    c = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "ada@example.com"}
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=c.pk,
        channel="email",
        purpose="marketing",
        status="opted_in",
        source="form",
    )
    return c


def delivered(workspace, person):
    registry.register("email", lambda: FakeAdapter("email"))
    message = gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel="email",
        body="Hello",
        subject="Hi",
        purpose="transactional",
    ).message
    message.refresh_from_db()
    return message


def mail_bytes(message_id="<hook-1@their.test>") -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "Bo <bo@example.com>", "hi@acme.test", "Hello"
    msg["Message-ID"], msg["Date"] = message_id, "Tue, 18 Nov 2026 10:00:00 -0500"
    msg.set_content("Do you ship to Ottawa?")
    return msg.as_bytes()


def post(public_id, body, signature, client=None):
    return (client or Client()).post(
        f"/api/v1/crm-public/hooks/{public_id}",
        data=body,
        content_type="application/octet-stream",
        HTTP_X_SIGNATURE=signature,
    )


@pytest.fixture
def inbound(workspace):
    return services.create_endpoint(workspace.pk, kind=WebhookKind.INBOUND_EMAIL, name="Mail")


def test_a_signed_email_is_stored_once_and_archived(workspace, inbound):
    body = mail_bytes()
    sig = services.sign(inbound.secret, body)
    assert post(inbound.endpoint.public_id, body, sig).status_code == 202
    assert post(inbound.endpoint.public_id, body, sig).status_code == 202  # sent twice
    assert Message.objects.for_workspace(workspace.pk).count() == 1
    row = WebhookDelivery.objects.get()
    assert row.status == DeliveryStatus.PROCESSED and bytes(row.body) == body
    inbound.endpoint.refresh_from_db()
    assert inbound.endpoint.last_received_at is not None


@pytest.mark.parametrize(
    "mangle",
    [
        lambda sig, secret, body: "",
        lambda sig, secret, body: "garbage",
        lambda sig, secret, body: sig[:-1] + ("0" if sig[-1] != "0" else "1"),
        lambda sig, secret, body: services.sign(secret, body, timestamp=int(time.time()) - 4000),
        lambda sig, secret, body: services.sign("other-secret", body),
    ],
)
def test_bad_signatures_are_refused_and_nothing_is_stored(workspace, inbound, mangle):
    body = mail_bytes()
    sig = mangle(services.sign(inbound.secret, body), inbound.secret, body)
    assert post(inbound.endpoint.public_id, body, sig).status_code == 403
    assert WebhookDelivery.objects.count() == 0 and Message.objects.count() == 0


def test_a_changed_body_does_not_match_its_signature(workspace, inbound):
    sig = services.sign(inbound.secret, mail_bytes())
    assert post(inbound.endpoint.public_id, mail_bytes("<other@x.test>"), sig).status_code == 403


def test_unknown_malformed_and_disabled_endpoints_look_the_same(workspace, inbound):
    body = mail_bytes()
    sig = services.sign(inbound.secret, body)
    assert post("nope", body, sig).status_code == 404
    assert post(f"{uuid.uuid4().hex}.abc", body, sig).status_code == 404
    services.set_active(workspace.pk, inbound.endpoint.pk, False)
    assert post(inbound.endpoint.public_id, body, sig).status_code == 404


def test_secrets_are_sealed_and_rotation_replaces_them(workspace, inbound):
    endpoint = inbound.endpoint
    assert inbound.secret.encode() not in bytes(endpoint.secret_ciphertext)
    body = mail_bytes()
    old = services.sign(inbound.secret, body)
    fresh = services.rotate_secret(workspace.pk, endpoint.pk)
    assert post(endpoint.public_id, body, old).status_code == 403
    assert post(endpoint.public_id, body, services.sign(fresh.secret, body)).status_code == 202


def test_an_endpoint_id_from_another_workspace_cannot_be_used_as_its_own(workspace, owner, inbound):
    other = create_workspace(name="Other Co", owner=owner)
    stolen = f"{other.pk.hex}.{inbound.endpoint.public_id.split('.', 1)[1]}"
    body = mail_bytes()
    assert post(stolen, body, services.sign(inbound.secret, body)).status_code == 404


def test_oversized_bodies_are_refused(workspace, inbound, settings):
    settings.CRM_WEBHOOK_MAX_BODY_BYTES = 10
    body = mail_bytes()
    assert (
        post(inbound.endpoint.public_id, body, services.sign(inbound.secret, body)).status_code
        == 422
    )


def test_bounce_and_complaint_events_update_the_message(workspace, person):
    message = delivered(workspace, person)
    created = services.create_endpoint(workspace.pk, kind=WebhookKind.EMAIL_EVENTS, name="Events")
    body = json.dumps(
        {"type": "bounce", "provider_message_id": message.provider_message_id, "hard": True}
    ).encode()
    assert (
        post(created.endpoint.public_id, body, services.sign(created.secret, body)).status_code
        == 202
    )
    message.refresh_from_db()
    assert (message.status, message.error_code) == ("failed", "bounced")
    assert suppression.is_suppressed(workspace.pk, "email", "ada@example.com")


def test_a_malformed_event_is_archived_as_failed(workspace):
    created = services.create_endpoint(workspace.pk, kind=WebhookKind.EMAIL_EVENTS, name="Events")
    body = b'{"type": "mystery"}'
    assert (
        post(created.endpoint.public_id, body, services.sign(created.secret, body)).status_code
        == 202
    )
    assert WebhookDelivery.objects.get().status == DeliveryStatus.FAILED


def test_endpoint_validation(workspace):
    with pytest.raises(ValidationFailedError):
        services.create_endpoint(workspace.pk, kind="nope", name="x")
    with pytest.raises(ValidationFailedError):
        services.create_endpoint(workspace.pk, kind=WebhookKind.INBOUND_EMAIL, name="  ")


# ---- management API ------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def logged(user):
    c = Client()
    c.force_login(user)
    return c


def test_api_roundtrip_shows_the_secret_only_once(workspace, owner):
    admin = logged(owner)
    made = api(admin, "post", "/webhooks", workspace, {"kind": "inbound_email", "name": "Mail"})
    assert made.status_code == 201 and made.json()["secret"]
    listed = api(admin, "get", "/webhooks", workspace).json()
    assert len(listed) == 1 and "secret" not in listed[0]
    wid = made.json()["id"]
    assert (
        api(admin, "post", f"/webhooks/{wid}/rotate", workspace).json()["secret"]
        != made.json()["secret"]
    )
    assert (
        api(admin, "patch", f"/webhooks/{wid}", workspace, {"is_active": False}).json()["is_active"]
        is False
    )
    assert api(admin, "get", f"/webhooks/{wid}/deliveries", workspace).json() == []
    assert api(admin, "delete", f"/webhooks/{wid}", workspace).status_code == 204


def test_api_permissions_and_cross_tenant(workspace, owner, inbound):
    viewer = logged(make_user("v@example.com", workspace, Role.VIEWER))
    admin = logged(owner)
    assert api(viewer, "get", "/webhooks", workspace).status_code == 403
    assert (
        api(
            viewer, "post", "/webhooks", workspace, {"kind": "inbound_email", "name": "x"}
        ).status_code
        == 403
    )
    other = create_workspace(name="Other Co", owner=owner)
    wid = inbound.endpoint.pk
    for method, path, payload in (
        ("post", f"/webhooks/{wid}/rotate", None),
        ("patch", f"/webhooks/{wid}", {"is_active": False}),
        ("delete", f"/webhooks/{wid}", None),
        ("get", f"/webhooks/{wid}/deliveries", None),
    ):
        assert api(admin, method, path, other, payload).status_code == 404, path
    assert api(admin, "get", "/webhooks", other).json() == []
    with pytest.raises(NotFoundError):
        services.get_endpoint(other.pk, wid)


def test_busy_webhooks_do_not_use_up_the_allowance_of_other_public_endpoints(workspace, inbound):
    """Throttle history is kept per scope; sharing one would lock people out of sign-in."""
    body = mail_bytes()
    for _ in range(35):
        post(inbound.endpoint.public_id, body, "bad")
    assert Client().get("/api/v1/crm-public/unsubscribe/not-a-token").status_code == 404
