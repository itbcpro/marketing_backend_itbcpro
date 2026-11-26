import json
from datetime import UTC, datetime
from email.message import EmailMessage

import pytest
from django.core import mail, signing
from django.test import Client

from audit.actors import Actor
from conftest import make_user
from crm.channels.email_smtp import SmtpEmailAdapter
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.inbox.models import Conversation, Message
from crm.sending import feedback, gateway, inbound_email, registry, suppression, unsubscribe
from crm.sending.models import Suppression
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def smtp():
    registry.register("email", SmtpEmailAdapter)
    return SmtpEmailAdapter


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


def send(workspace, person, **extra):
    return gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel="email",
        body="Hello",
        subject="Hi",
        **extra,
    )


# ---- unsubscribe -----------------------------------------------------------------------------


def test_email_carries_one_click_unsubscribe_headers(workspace, person, smtp):
    outcome = send(workspace, person, purpose="marketing")
    sent = mail.outbox[-1]
    link = sent.extra_headers["List-Unsubscribe"].strip("<>")
    assert "/crm-public/unsubscribe/" in link
    assert sent.extra_headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert Message.objects.get(pk=outcome.message.pk).status == "sent"


def test_the_link_shows_a_masked_address_and_posting_unsubscribes(workspace, person, smtp):
    send(workspace, person, purpose="marketing")
    token = mail.outbox[-1].extra_headers["List-Unsubscribe"].strip("<>").rsplit("/", 1)[-1]
    client = Client(enforce_csrf_checks=True)  # providers do not carry a CSRF token
    info = client.get(f"/api/v1/crm-public/unsubscribe/{token}")
    assert info.json() == {"address": "a***@example.com", "unsubscribed": False}
    assert (
        client.get(f"/api/v1/crm-public/unsubscribe/{token}").json()["unsubscribed"] is False
    )  # GET changes nothing
    assert client.post(f"/api/v1/crm-public/unsubscribe/{token}").status_code == 204
    assert (
        client.post(f"/api/v1/crm-public/unsubscribe/{token}").status_code == 204
    )  # repeating is fine
    assert client.get(f"/api/v1/crm-public/unsubscribe/{token}").json()["unsubscribed"] is True
    assert send(workspace, person, purpose="marketing").reason == "opted_out"
    assert send(workspace, person, purpose="transactional").blocked is False  # only marketing stops
    rows = [e for e in consent.history(workspace.pk, person.pk) if e.source == "link"]
    assert len(rows) == 1 and rows[0].evidence == {"via": "unsubscribe_link"}


def test_tampered_expired_and_foreign_links_do_not_work(workspace, person, owner):
    token = unsubscribe.make_token(workspace.pk, person.pk)
    client = Client()
    assert client.post(f"/api/v1/crm-public/unsubscribe/{token}x").status_code == 404
    assert client.post("/api/v1/crm-public/unsubscribe/garbage").status_code == 404
    forged = signing.dumps(
        {"w": str(workspace.pk), "c": str(person.pk), "ch": "email"}, salt="another"
    )
    assert client.post(f"/api/v1/crm-public/unsubscribe/{forged}").status_code == 404
    assert consent.states_of(workspace.pk, person.pk)[0].status == "opted_in"


def test_a_link_for_an_erased_contact_still_answers_politely(workspace, person):
    token = unsubscribe.make_token(workspace.pk, person.pk)
    contacts.erase_contact(workspace_id=workspace.pk, actor=SYSTEM, contact_id=person.pk)
    client = Client()
    assert client.post(f"/api/v1/crm-public/unsubscribe/{token}").status_code == 204
    assert client.get(f"/api/v1/crm-public/unsubscribe/{token}").json()["address"] == "***"


def test_a_link_only_affects_its_own_workspace(workspace, person, owner):
    other = create_workspace(name="Other Co", owner=owner)
    twin = contacts.create_contact(
        workspace_id=other.pk, actor=SYSTEM, data={"email": "ada@example.com"}
    )
    consent.record(
        workspace_id=other.pk,
        actor=SYSTEM,
        contact_id=twin.pk,
        channel="email",
        purpose="marketing",
        status="opted_in",
        source="form",
    )
    Client().post(
        f"/api/v1/crm-public/unsubscribe/{unsubscribe.make_token(workspace.pk, person.pk)}"
    )
    assert consent.states_of(other.pk, twin.pk)[0].status == "opted_in"


# ---- suppression -----------------------------------------------------------------------------


def test_suppressed_addresses_are_never_mailed_even_with_consent(workspace, person, smtp):
    suppression.suppress(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        channel="email",
        address=" ADA@example.com ",
        reason="manual",
    )
    outcome = send(workspace, person, purpose="transactional")
    assert (outcome.blocked, outcome.reason) == (True, "suppressed")
    assert mail.outbox == []


def test_suppression_is_idempotent_and_removable_and_scoped(workspace, owner, person):
    first, created = suppression.suppress(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        channel="email",
        address="x@example.com",
        reason="hard_bounce",
    )
    again, created_again = suppression.suppress(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        channel="email",
        address="X@example.com",
        reason="manual",
    )
    assert created and not created_again and first.pk == again.pk and again.reason == "hard_bounce"
    other = create_workspace(name="Other Co", owner=owner)
    assert not suppression.is_suppressed(other.pk, "email", "x@example.com")
    suppression.remove(workspace_id=workspace.pk, actor=SYSTEM, suppression_id=first.pk)
    assert not suppression.is_suppressed(workspace.pk, "email", "x@example.com")


def test_suppression_api(workspace, owner):
    admin, viewer = Client(), Client()
    admin.force_login(owner)
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    headers = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    body = json.dumps({"address": "Bad@Example.com"})
    created = admin.post(
        "/api/v1/crm/suppressions", data=body, content_type="application/json", **headers
    )
    assert created.status_code == 201 and created.json()["address"] == "bad@example.com"
    assert (
        viewer.post(
            "/api/v1/crm/suppressions", data=body, content_type="application/json", **headers
        ).status_code
        == 403
    )
    assert len(viewer.get("/api/v1/crm/suppressions", **headers).json()) == 1
    bad = admin.post(
        "/api/v1/crm/suppressions",
        data=json.dumps({"address": "nope"}),
        content_type="application/json",
        **headers,
    )
    assert bad.status_code == 422
    other = create_workspace(name="Other Co", owner=owner)
    assert admin.get("/api/v1/crm/suppressions", HTTP_X_WORKSPACE_ID=str(other.pk)).json() == []
    assert (
        admin.delete(
            f"/api/v1/crm/suppressions/{created.json()['id']}", HTTP_X_WORKSPACE_ID=str(other.pk)
        ).status_code
        == 404
    )
    assert (
        admin.delete(f"/api/v1/crm/suppressions/{created.json()['id']}", **headers).status_code
        == 204
    )


# ---- bounces and complaints ------------------------------------------------------------------


def delivered(workspace, person):
    registry.register("email", lambda: FakeAdapter("email"))
    message = send(workspace, person, purpose="transactional").message
    message.refresh_from_db()
    inbox.apply_status(
        workspace_id=workspace.pk,
        channel="email",
        provider_message_id=message.provider_message_id,
        status="delivered",
        provider_ts=datetime.now(UTC),
    )
    return message


def test_a_hard_bounce_fails_the_message_even_after_delivery_and_suppresses(
    workspace, person, smtp
):
    message = delivered(workspace, person)
    feedback.record_bounce(
        workspace_id=workspace.pk,
        channel="email",
        provider_message_id=message.provider_message_id,
        hard=True,
    )
    message.refresh_from_db()
    assert (message.status, message.error_code) == ("failed", "bounced")
    assert suppression.is_suppressed(workspace.pk, "email", "ada@example.com")
    registry.register("email", lambda: FakeAdapter("email"))
    assert send(workspace, person).reason == "suppressed"


def test_a_soft_bounce_does_not_suppress(workspace, person, smtp):
    message = delivered(workspace, person)
    feedback.record_bounce(
        workspace_id=workspace.pk,
        channel="email",
        provider_message_id=message.provider_message_id,
        hard=False,
    )
    assert not Suppression.objects.exists()


def test_a_complaint_suppresses_and_stops_marketing(workspace, person, smtp):
    message = delivered(workspace, person)
    feedback.record_complaint(
        workspace_id=workspace.pk, channel="email", provider_message_id=message.provider_message_id
    )
    assert suppression.is_suppressed(workspace.pk, "email", "ada@example.com")
    assert (
        consent.decide(
            workspace_id=workspace.pk, contact_id=person.pk, channel="email", purpose="marketing"
        ).reason
        == "opted_out"
    )


def test_unknown_message_ids_are_ignored(workspace):
    assert (
        feedback.record_bounce(
            workspace_id=workspace.pk, channel="email", provider_message_id="nope", hard=True
        )
        is None
    )
    assert (
        feedback.record_complaint(
            workspace_id=workspace.pk, channel="email", provider_message_id="nope"
        )
        is None
    )


# ---- inbound email ---------------------------------------------------------------------------


def raw_mail(
    subject="Question",
    body="Do you ship to Ottawa?",
    sender="Ada L <Ada@Example.com>",
    extra=None,
    message_id="<in-1@their.test>",
):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "hello@acme.test", subject
    msg["Message-ID"], msg["Date"] = message_id, "Tue, 18 Nov 2026 10:00:00 -0500"
    msg["In-Reply-To"] = "<ours-1@acme.test>"
    for key, value in (extra or {}).items():
        msg[key] = value
    msg.set_content(body)
    return msg.as_bytes()


def test_a_customer_email_becomes_an_inbound_message_without_quoted_history(workspace):
    body = "Yes please.\n\nOn Mon, Nov 17, 2026 at 9:00 AM Acme <hello@acme.test> wrote:\n> Do you want a quote?\n> Reply here."
    kind, message = inbound_email.ingest(workspace_id=workspace.pk, raw=raw_mail(body=body))
    assert kind == "message"
    assert (message.body, message.subject, message.channel, message.direction) == (
        "Yes please.",
        "Question",
        "email",
        "inbound",
    )
    assert message.provider_ts == datetime(2026, 11, 18, 15, 0, tzinfo=UTC)
    contact = contacts.find_contact(workspace.pk, "email", "ada@example.com")
    assert contact.first_name == "Ada" and Conversation.objects.get().contact_id == contact.pk


def test_the_same_email_twice_is_stored_once(workspace):
    inbound_email.ingest(workspace_id=workspace.pk, raw=raw_mail())
    inbound_email.ingest(workspace_id=workspace.pk, raw=raw_mail())
    assert Message.objects.count() == 1


def test_emails_without_a_message_id_are_still_deduplicated(workspace):
    raw = raw_mail(message_id="")
    inbound_email.ingest(workspace_id=workspace.pk, raw=raw)
    inbound_email.ingest(workspace_id=workspace.pk, raw=raw)
    assert Message.objects.count() == 1


@pytest.mark.parametrize(
    "extra",
    [{"Auto-Submitted": "auto-replied"}, {"Precedence": "bulk"}, {"X-Autoreply": "yes"}],
)
def test_auto_replies_do_not_count_as_the_customer_answering(workspace, extra):
    kind, message = inbound_email.ingest(workspace_id=workspace.pk, raw=raw_mail(extra=extra))
    assert (kind, message) == ("auto_reply", None) and not Message.objects.exists()


def test_out_of_office_subjects_are_auto_replies(workspace):
    assert (
        inbound_email.ingest(
            workspace_id=workspace.pk, raw=raw_mail(subject="Out of office until Monday")
        )[0]
        == "auto_reply"
    )


def test_html_only_mail_is_converted_to_text(workspace):
    msg = EmailMessage()
    msg["From"], msg["Subject"], msg["Message-ID"] = "b@example.com", "Hi", "<h1@x.test>"
    msg.set_content("<p>Hello <b>there</b></p>", subtype="html")
    _, message = inbound_email.ingest(workspace_id=workspace.pk, raw=msg.as_bytes())
    assert message.body == "Hello there"


def test_attachments_are_listed_not_stored(workspace):
    msg = EmailMessage()
    msg["From"], msg["Subject"], msg["Message-ID"] = "c@example.com", "File", "<f1@x.test>"
    msg.set_content("see attached")
    msg.add_attachment(
        b"%PDF-1.4 data", maintype="application", subtype="pdf", filename="quote.pdf"
    )
    _, message = inbound_email.ingest(workspace_id=workspace.pk, raw=msg.as_bytes())
    assert message.attachments == [
        {"filename": "quote.pdf", "content_type": "application/pdf", "size": 13}
    ]


def bounce_report(original_id, recipient="ada@example.com", status="5.1.1"):
    outer = EmailMessage()
    outer["From"], outer["To"], outer["Subject"] = (
        "MAILER-DAEMON@mx.test",
        "hello@acme.test",
        "Undelivered Mail",
    )
    outer.set_content("Delivery failed")
    raw = outer.as_bytes().decode()
    report = (
        raw.replace(
            "Content-Type: text/plain",
            'Content-Type: multipart/report; report-type=delivery-status; boundary="B"',
        ).split("\n\n", 1)[0]
        + f"\n\n--B\nContent-Type: text/plain\n\nDelivery failed\n--B\nContent-Type: message/delivery-status\n\n"
        f"Reporting-MTA: dns; mx.test\n\nFinal-Recipient: rfc822; {recipient}\nAction: failed\nStatus: {status}\n\n"
        f"--B\nContent-Type: message/rfc822\n\nMessage-ID: {original_id}\nSubject: Hi\n\nbody\n--B--\n"
    )
    return report.encode()


def test_a_bounce_report_marks_the_message_and_suppresses_the_address(workspace, person, smtp):
    message = delivered(workspace, person)
    kind, updated = inbound_email.ingest(
        workspace_id=workspace.pk, raw=bounce_report(message.provider_message_id)
    )
    assert kind == "bounce" and updated.error_code == "bounced"
    assert suppression.is_suppressed(workspace.pk, "email", "ada@example.com")


def test_a_bounce_for_an_unknown_message_still_suppresses_a_hard_failure(workspace):
    kind, updated = inbound_email.ingest(
        workspace_id=workspace.pk, raw=bounce_report("<gone@x.test>", "old@example.com")
    )
    assert (kind, updated) == ("bounce", None)
    assert suppression.is_suppressed(workspace.pk, "email", "old@example.com")
    inbound_email.ingest(
        workspace_id=workspace.pk,
        raw=bounce_report("<gone2@x.test>", "slow@example.com", status="4.2.2"),
    )
    assert not suppression.is_suppressed(workspace.pk, "email", "slow@example.com")


def test_garbage_senders_are_rejected(workspace):
    from core.errors import ValidationFailedError

    with pytest.raises(ValidationFailedError):
        inbound_email.ingest(workspace_id=workspace.pk, raw=raw_mail(sender="not-an-address"))
    assert not Message.objects.exists()
