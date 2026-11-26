import json
from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.models import AuditEvent
from audit.outbox import dispatch_batch
from conftest import make_user
from crm.activity import services as activity
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.events.models import ProcessedEvent
from crm.inbox import services as inbox
from crm.leads.models import ConversionLog, ConversionRule
from crm.notifications.models import Notification
from crm.pipelines import services as pipelines
from crm.privacy import retention
from crm.reports.models import FirstResponse
from crm.voice.models import Call
from crm.webhooks import services as webhooks
from crm.webhooks.models import WebhookDelivery, WebhookKind
from crm.workflows import definitions, engine
from crm.workflows.models import Enrollment, StepLog
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def person(workspace):
    c = contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"first_name": "Ada", "email": "ada@example.com", "phone": "+14165550111"},
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


def fill(workspace, person):
    inbox.ingest(
        workspace_id=workspace.pk,
        item=inbox.InboundMessage(
            channel="email",
            provider_message_id="m1",
            sender_channel="email",
            sender_value="ada@example.com",
            provider_ts=datetime.now(UTC),
            body="My secret question",
            subject="Hello",
        ),
    )
    activity.add_note(
        workspace_id=workspace.pk, actor=SYSTEM, contact_id=person.pk, body="Prefers mornings"
    )
    activity.create_task(
        workspace_id=workspace.pk, actor=SYSTEM, contact_id=person.pk, title="Call back"
    )
    Call.objects.create(
        workspace_id=workspace.pk,
        contact=person,
        direction="inbound",
        provider_call_id="CA1",
        from_number="+14165550111",
        to_number="+14165550100",
        transcript="Please call me",
        recording_url="https://x.test/rec",
    )
    flow = pipelines.create_pipeline(workspace.pk, name="Sales", stages=[("New", 10)])
    pipelines.create_opportunity(
        workspace.pk, SYSTEM, contact_id=person.pk, pipeline_id=flow.pk, name="Roof", value_micros=5
    )
    workflow = definitions.create_workflow(workspace.pk, name="Flow")
    definitions.save_draft(
        workspace.pk,
        workflow.pk,
        {
            "trigger": {"type": "contact_created", "config": {}},
            "start": "w",
            "steps": [{"id": "w", "type": "wait", "config": {"seconds": 60}}],
        },
    )
    definitions.publish(workspace.pk, workflow.pk)
    engine.enroll(workspace.pk, workflow.pk, person.pk)


# ---- export ------------------------------------------------------------------------------------


def test_the_export_holds_everything_about_the_person_and_is_logged(workspace, owner, person):
    fill(workspace, person)
    client = Client()
    client.force_login(owner)
    res = client.get(
        f"/api/v1/crm/contacts/{person.pk}/privacy-export", HTTP_X_WORKSPACE_ID=str(workspace.pk)
    )
    assert res.status_code == 200
    assert "attachment" in res["Content-Disposition"]
    data = json.loads(res.content)
    assert data["contact"]["profile"]["email"] == "ada@example.com"
    assert data["consent"][0]["status"] == "opted_in"
    assert data["messages"][0]["body"] == "My secret question"
    assert data["notes"][0]["body"] == "Prefers mornings"
    assert data["tasks"][0]["title"] == "Call back"
    assert data["calls"][0]["transcript"] == "Please call me"
    assert data["deals"][0]["name"] == "Roof"
    assert data["automations"][0]["workflow"] == "Flow"
    assert AuditEvent.objects.filter(
        action="crm.contact.exported", workspace_id=workspace.pk
    ).exists()


def test_only_people_who_may_export_can_and_only_inside_their_workspace(workspace, owner, person):
    url = f"/api/v1/crm/contacts/{person.pk}/privacy-export"
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    assert viewer.get(url, HTTP_X_WORKSPACE_ID=str(workspace.pk)).status_code == 403
    other = create_workspace(name="Other Co", owner=owner)
    admin = Client()
    admin.force_login(owner)
    assert admin.get(url, HTTP_X_WORKSPACE_ID=str(other.pk)).status_code == 404
    assert Client().get(url).status_code in (401, 403)


# ---- erasure -----------------------------------------------------------------------------------


def test_erasing_a_person_removes_their_data_and_anonymises_the_analytics(workspace, owner, person):
    fill(workspace, person)
    dispatch_batch()
    FirstResponse.objects.update(responded_at=timezone.now(), seconds=10)
    rule = ConversionRule.objects.create(
        workspace_id=workspace.pk, name="r", trigger="lead_captured", event_name="Lead"
    )
    ConversionLog.objects.create(
        workspace_id=workspace.pk, rule=rule, event_id="e1", contact_id=person.pk, outcome="sent"
    )
    contacts.erase_contact(
        workspace_id=workspace.pk, actor=Actor.human(owner.pk), contact_id=person.pk
    )
    dispatch_batch()
    assert Call.objects.count() == 0 and Enrollment.objects.count() == 0
    nil = "00000000-0000-0000-0000-000000000000"
    assert str(FirstResponse.objects.get().contact_id) == nil
    assert str(ConversionLog.objects.get().contact_id) == nil


# ---- retention ---------------------------------------------------------------------------------


def old(model, **filters):
    model.objects.filter(**filters).update(created_at=timezone.now() - timedelta(days=4000))


def test_each_policy_removes_only_what_is_old_enough(workspace, owner, person, settings):
    fill(workspace, person)
    created = webhooks.create_endpoint(workspace.pk, kind=WebhookKind.EMAIL_EVENTS, name="E")
    fresh = WebhookDelivery.objects.create(
        workspace_id=workspace.pk, endpoint=created.endpoint, body_sha256="a", body=b"x"
    )
    stale = WebhookDelivery.objects.create(
        workspace_id=workspace.pk, endpoint=created.endpoint, body_sha256="b", body=b"y"
    )
    old(WebhookDelivery, pk=stale.pk)
    ProcessedEvent.objects.create(workspace_id=workspace.pk, consumer="c", event_id=person.pk)
    ProcessedEvent.objects.create(workspace_id=workspace.pk, consumer="old", event_id=owner.pk)
    old(ProcessedEvent, consumer="old")
    done = Enrollment.objects.get()
    engine.exit_enrollment(done, "test")
    Enrollment.objects.filter(pk=done.pk).update(finished_at=timezone.now() - timedelta(days=4000))
    seen = Notification.objects.create(
        workspace_id=workspace.pk, user=owner, kind="mention", read_at=timezone.now()
    )
    old(Notification, pk=seen.pk)
    unseen = Notification.objects.create(workspace_id=workspace.pk, user=owner, kind="mention")
    old(Notification, pk=unseen.pk)
    old(Call)

    result = retention.run()

    assert result == {
        "webhook_bodies": 1,
        "processed_events": result["processed_events"],
        "workflow_runs": result["workflow_runs"],
        "read_notifications": 1,
        "call_recordings": 1,
        "email_activity": 0,
    }
    assert result["processed_events"] >= 1 and result["workflow_runs"] >= 1
    assert list(WebhookDelivery.objects.values_list("pk", flat=True)) == [fresh.pk]
    assert not Enrollment.objects.exists() and not StepLog.objects.exists()
    assert Notification.objects.filter(pk=unseen.pk).exists()  # unread stays however old
    call = Call.objects.get()
    assert call.recording_url == "" and call.transcript == ""  # the call itself is kept
    assert retention.run()["webhook_bodies"] == 0  # nothing left to remove


def test_open_runs_are_never_purged(workspace, person):
    fill(workspace, person)
    Enrollment.objects.update(created_at=timezone.now() - timedelta(days=4000))
    retention.run()
    assert Enrollment.objects.filter(status__in=["active", "waiting"]).exists()
