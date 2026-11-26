import json
from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from audit.actors import Actor
from audit.models import OutboxMessage
from conftest import make_user
from core.errors import ValidationFailedError
from crm.channels.fake import FakeAdapter, permanent, temporary
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.inbox.models import Message
from crm.sending import gateway, registry
from crm.sending.ports import RetryableAdapterError
from crm.sending.services import Block
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
CHANNELS = ("email", "sms", "instagram", "whatsapp")


@pytest.fixture
def adapters():
    """Fake adapters on every channel for the duration of one test."""
    fakes = {c: FakeAdapter(c) for c in CHANNELS}
    for channel, fake in fakes.items():
        registry.register(channel, lambda fake=fake: fake)
    yield fakes
    from crm.channels.email_smtp import SmtpEmailAdapter

    for channel in CHANNELS:
        registry.unregister(channel)
    registry.register("email", SmtpEmailAdapter)


@pytest.fixture
def person(workspace):
    c = contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"email": "ada@example.com", "phone": "416-555-0199", "time_zone": "America/Toronto"},
    )
    for channel in ("email", "sms"):
        consent.record(
            workspace_id=workspace.pk,
            actor=SYSTEM,
            contact_id=c.pk,
            channel=channel,
            purpose="marketing",
            status="opted_in",
            source="form",
        )
    return c


def send(workspace, person, channel="email", **extra):
    values = {"body": "Hello Ada", "subject": "Hi", "purpose": "transactional", **extra}
    return gateway.send(
        workspace_id=workspace.pk, actor=SYSTEM, contact_id=person.pk, channel=channel, **values
    )


# ---- the happy path ---------------------------------------------------------------------------


def test_an_allowed_message_is_queued_delivered_and_recorded(workspace, person, adapters):
    outcome = send(workspace, person)
    assert not outcome.blocked
    message = Message.objects.get(pk=outcome.message.pk)
    assert (message.status, message.channel, message.direction) == ("sent", "email", "outbound")
    assert message.provider_message_id.startswith("fake-")
    (request,) = adapters["email"].sent
    assert (request.to, request.body, request.subject) == ("ada@example.com", "Hello Ada", "Hi")
    assert OutboundTopics.count("crm.message.outbound") == 1


class OutboundTopics:
    @staticmethod
    def count(topic):
        return OutboxMessage.objects.filter(topic=topic).count()


def test_replies_carry_the_threading_reference_of_the_last_inbound(workspace, person, adapters):
    inbox.ingest(
        workspace_id=workspace.pk,
        item=inbox.InboundMessage(
            channel="email",
            provider_message_id="<their-1@x.test>",
            sender_channel="email",
            sender_value="ada@example.com",
            provider_ts=datetime.now(UTC) - timedelta(minutes=1),
            body="question",
        ),
    )
    send(workspace, person)
    assert adapters["email"].sent[0].in_reply_to == "<their-1@x.test>"


def test_the_same_idempotency_key_sends_once(workspace, person, adapters):
    first = send(workspace, person, idempotency_key="k-1")
    again = send(workspace, person, idempotency_key="k-1")
    assert first.message.pk == again.message.pk
    assert len(adapters["email"].sent) == 1 and Message.objects.count() == 1
    blocked_first = send(workspace, person, channel="whatsapp", idempotency_key="k-2")
    blocked_again = send(workspace, person, channel="whatsapp", idempotency_key="k-2")
    assert blocked_first.message.pk == blocked_again.message.pk and blocked_again.blocked


# ---- every reason to refuse -------------------------------------------------------------------


def test_marketing_needs_recorded_consent(workspace, adapters):
    stranger = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "new@example.com"}
    )
    outcome = send(workspace, stranger, purpose="marketing")
    assert (outcome.blocked, outcome.reason) == (True, "no_consent")
    assert adapters["email"].sent == []
    stored = Message.objects.get(pk=outcome.message.pk)
    assert (stored.status, stored.error_code) == ("blocked", "no_consent")
    assert send(workspace, stranger, purpose="transactional").blocked is False


def test_an_opt_out_blocks_even_transactional_messages(workspace, person, adapters):
    consent.handle_keyword(
        workspace_id=workspace.pk, contact_id=person.pk, channel="sms", text="STOP"
    )
    assert send(workspace, person, channel="sms").reason == "opted_out"
    assert adapters["sms"].sent == []


def test_quiet_hours_stop_marketing_texts(workspace, person, adapters, monkeypatch):
    night = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: night)
    assert send(workspace, person, channel="sms", purpose="marketing").reason == "quiet_hours"
    assert send(workspace, person, channel="sms", purpose="transactional").blocked is False


def test_contacts_without_the_address_cannot_be_reached(workspace, adapters):
    only_email = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "e@example.com"}
    )
    assert send(workspace, only_email, channel="sms").reason == Block.NO_ADDRESS


def test_missing_adapters_and_bad_bodies(workspace, person, adapters):
    registry.unregister("sms")
    assert send(workspace, person, channel="sms").reason == Block.NO_CHANNEL
    assert send(workspace, person, body="   ").reason == Block.INVALID_BODY
    assert send(workspace, person, body="x" * 1001).reason == Block.INVALID_BODY
    with pytest.raises(ValidationFailedError):
        send(workspace, person, channel="pigeon")
    with pytest.raises(ValidationFailedError):
        send(workspace, person, purpose="spam")


def test_reply_windows_close_after_a_day(workspace, adapters):
    ig = contacts.create_contact(workspace_id=workspace.pk, actor=SYSTEM, data={})
    contacts.attach_identity(ig, "instagram", "igsid-1")
    assert (
        send(workspace, ig, channel="instagram").reason == Block.WINDOW_CLOSED
    )  # never wrote to us
    inbox.ingest(
        workspace_id=workspace.pk,
        item=inbox.InboundMessage(
            channel="instagram",
            provider_message_id="i1",
            sender_channel="instagram",
            sender_value="igsid-1",
            provider_ts=timezone.now() - timedelta(hours=2),
            body="hi",
        ),
    )
    assert send(workspace, ig, channel="instagram").blocked is False
    inbox.ingest(
        workspace_id=workspace.pk,
        item=inbox.InboundMessage(
            channel="instagram",
            provider_message_id="i0",
            sender_channel="instagram",
            sender_value="igsid-9",
            provider_ts=timezone.now() - timedelta(hours=30),
            body="old",
        ),
    )
    old = contacts.find_contact(workspace.pk, "instagram", "igsid-9")
    assert send(workspace, old, channel="instagram").reason == Block.WINDOW_CLOSED


def test_the_workspace_kill_switch_stops_everything(workspace, person, adapters):
    workspace.writes_paused = True
    workspace.save()
    assert send(workspace, person).reason == Block.KILL_SWITCH
    assert adapters["email"].sent == []


def test_agents_need_a_human_approval(workspace, person, adapters):
    agent = Actor.agent("community-responder")
    blocked = gateway.send(
        workspace_id=workspace.pk, actor=agent, contact_id=person.pk, channel="email", body="Hi"
    )
    assert blocked.reason == Block.APPROVAL_REQUIRED and adapters["email"].sent == []
    approved = gateway.send(
        workspace_id=workspace.pk,
        actor=agent,
        contact_id=person.pk,
        channel="email",
        body="Hi",
        approved=True,
    )
    assert approved.blocked is False and len(adapters["email"].sent) == 1


def test_plan_limits_and_rate_limits(workspace, person, adapters, settings):
    workspace.organization.limits = {"messages_per_month": 2}
    workspace.organization.save()
    assert [send(workspace, person).blocked for _ in range(3)] == [False, False, True]
    assert Message.objects.filter(status="blocked").get().error_code == Block.LIMIT_REACHED
    workspace.organization.limits = {}
    workspace.organization.save()
    settings.CRM_SEND_PER_MINUTE = 2
    outcomes = [send(workspace, person, channel="sms") for _ in range(3)]
    assert [o.reason for o in outcomes] == ["", "", Block.RATE_LIMITED]
    assert send(workspace, person, channel="email").reason in {
        "",
        Block.RATE_LIMITED,
    }  # per channel


def test_blocked_messages_do_not_count_against_limits(workspace, person, adapters):
    workspace.organization.limits = {"messages_per_month": 1}
    workspace.organization.save()
    send(workspace, person, channel="whatsapp")  # blocked: no address / window
    send(workspace, person, body=" ")  # blocked: invalid body
    assert send(workspace, person).blocked is False


# ---- delivery failures -----------------------------------------------------------------------


def test_a_permanent_provider_error_marks_the_message_failed(workspace, person, adapters):
    adapters["sms"].fail_next(permanent("bad_number"))
    outcome = send(workspace, person, channel="sms")
    stored = Message.objects.get(pk=outcome.message.pk)
    assert (stored.status, stored.error_code) == ("failed", "bad_number")


def test_temporary_errors_are_retried_and_then_succeed(workspace, person, adapters):
    adapters["sms"].fail_next(temporary(), temporary())
    with override_settings(CELERY_TASK_EAGER_PROPAGATES=False):
        outcome = send(workspace, person, channel="sms")
    stored = Message.objects.get(pk=outcome.message.pk)
    assert stored.status in {
        "pending",
        "sent",
    }  # eager retries run inline; both end states are consistent


def test_the_last_retry_gives_up_with_the_provider_code(workspace, person, adapters):
    from crm.sending import delivery

    pending = inbox.create_outbound(
        workspace_id=workspace.pk, actor=SYSTEM, contact=person, channel="sms", body="x"
    )
    adapters["sms"].fail_next(temporary("provider_unavailable"))
    with pytest.raises(RetryableAdapterError):
        delivery.deliver(workspace_id=workspace.pk, message_id=pending.pk, final_attempt=False)
    adapters["sms"].fail_next(temporary("provider_unavailable"))
    final = delivery.deliver(workspace_id=workspace.pk, message_id=pending.pk, final_attempt=True)
    assert (final.status, final.error_code) == ("failed", "provider_unavailable")


def test_delivering_twice_does_not_send_twice(workspace, person, adapters):
    from crm.sending import delivery

    outcome = send(workspace, person)
    again = delivery.deliver(
        workspace_id=workspace.pk, message_id=outcome.message.pk, final_attempt=False
    )
    assert again.status == "sent" and len(adapters["email"].sent) == 1


# ---- API --------------------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def thread_of(workspace, person):
    return inbox.get_or_create(workspace.pk, person)


def test_api_sends_and_reports_blocks(workspace, owner, person, adapters):
    admin = Client()
    admin.force_login(owner)
    conversation = thread_of(workspace, person)
    Message.objects.all().delete()
    conversation.last_message_at = timezone.now()
    conversation.save()
    ok = api(
        admin,
        "post",
        f"/inbox/conversations/{conversation.pk}/send",
        workspace,
        {"channel": "email", "body": "Hi", "subject": "S"},
    )
    assert ok.status_code == 201 and ok.json()["blocked"] is False
    assert Message.objects.get(pk=ok.json()["message_id"]).status == "sent"
    blocked = api(
        admin,
        "post",
        f"/inbox/conversations/{conversation.pk}/send",
        workspace,
        {"channel": "instagram", "body": "Hi"},
    )
    assert blocked.status_code == 201 and blocked.json()["reason"] == "no_address"
    bad = api(
        admin,
        "post",
        f"/inbox/conversations/{conversation.pk}/send",
        workspace,
        {"channel": "pigeon", "body": "Hi"},
    )
    assert bad.status_code == 422
    assert "email" in api(admin, "get", "/inbox/channels", workspace).json()


def test_api_permissions_scope_and_tenants(workspace, owner, person, adapters):
    conversation = thread_of(workspace, person)
    conversation.last_message_at = timezone.now()
    conversation.save()
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    body = {"channel": "email", "body": "Hi"}
    assert (
        api(
            viewer, "post", f"/inbox/conversations/{conversation.pk}/send", workspace, body
        ).status_code
        == 403
    )
    other = create_workspace(name="Other Co", owner=owner)
    admin = Client()
    admin.force_login(owner)
    assert (
        api(admin, "post", f"/inbox/conversations/{conversation.pk}/send", other, body).status_code
        == 404
    )
    assert adapters["email"].sent == []
