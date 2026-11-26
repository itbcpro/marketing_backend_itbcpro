import json
from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.models import OutboxMessage
from audit.outbox import dispatch_batch
from conftest import make_user
from crm.activity import services as activity
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.pipelines import services as pipelines
from crm.reports import queries
from crm.reports.models import FirstResponse
from crm.sending import gateway, registry
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def fake_email():
    adapter = FakeAdapter("email")
    registry.register("email", lambda: adapter)
    return adapter


def window():
    now = timezone.now()
    return now - timedelta(days=1), now + timedelta(days=1)


def inbound(workspace, mid="m1", sender="ada@example.com"):
    return inbox.ingest(
        workspace_id=workspace.pk,
        item=inbox.InboundMessage(
            channel="email",
            provider_message_id=mid,
            sender_channel="email",
            sender_value=sender,
            provider_ts=datetime.now(UTC),
            body="Question",
        ),
    )


def reply(workspace, contact, actor, **kw):
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=contact.pk,
        channel="email",
        purpose="transactional",
        status="opted_in",
        source="form",
    )
    return gateway.send(
        workspace_id=workspace.pk,
        actor=actor,
        contact_id=contact.pk,
        channel="email",
        body="Answer",
        subject="Re",
        **kw,
    )


def age(row, seconds):
    """Make an event look older, so a measured wait is not zero."""
    FirstResponse.objects.filter(pk=row.pk).update(
        inbound_at=row.inbound_at - timedelta(seconds=seconds)
    )


def test_first_response_time_is_computed_when_the_first_reply_is_written(
    workspace, owner, fake_email
):
    result = inbound(workspace)
    dispatch_batch()
    opened = FirstResponse.objects.get()
    assert opened.responded_at is None
    age(opened, 90)
    inbound(workspace, mid="m2")  # a second message does not restart the clock
    dispatch_batch()
    assert FirstResponse.objects.count() == 1
    reply(workspace, result.contact, Actor.human(owner.pk))
    dispatch_batch()
    row = FirstResponse.objects.get()
    assert 90 <= row.seconds <= 100 and row.responder_type == "human"
    assert row.responder_id == str(owner.pk)
    # Another reply later changes nothing; a new customer message opens a new row.
    reply(workspace, result.contact, Actor.human(owner.pk), idempotency_key="again")
    dispatch_batch()
    assert FirstResponse.objects.get().seconds == row.seconds
    inbound(workspace, mid="m3")
    dispatch_batch()
    assert FirstResponse.objects.count() == 2


def test_blocked_replies_do_not_count_as_answers(workspace, owner, fake_email):
    result = inbound(workspace)
    dispatch_batch()
    # No consent recorded: the gateway blocks the reply, which must not close the clock.
    blocked = gateway.send(
        workspace_id=workspace.pk,
        actor=Actor.human(owner.pk),
        contact_id=result.contact.pk,
        channel="sms",
        body="Hi",
    )
    assert blocked.blocked
    dispatch_batch()
    assert FirstResponse.objects.get().responded_at is None


def test_the_same_events_twice_change_nothing(workspace, owner, fake_email):
    result = inbound(workspace)
    reply(workspace, result.contact, Actor.human(owner.pk))
    dispatch_batch()
    OutboxMessage.objects.update(processed_at=None)
    dispatch_batch()
    assert FirstResponse.objects.count() == 1


def test_response_time_summary_by_responder_and_channel(workspace, owner, fake_email):
    for index, wait in enumerate((60, 120, 600)):
        result = inbound(workspace, mid=f"m{index}", sender=f"p{index}@example.com")
        dispatch_batch()
        age(FirstResponse.objects.get(contact_id=result.contact.pk), wait)
        reply(workspace, result.contact, Actor.human(owner.pk))
        dispatch_batch()
    inbound(workspace, mid="waiting", sender="w@example.com")
    dispatch_batch()
    report = queries.response_time(workspace.pk, *window())
    assert report["count"] == 3 and report["waiting_now"] == 1
    assert 115 <= report["median_seconds"] <= 130
    assert report["by_responder"][0]["responder_type"] == "human"
    assert report["by_channel"][0]["channel"] == "email"


def test_delivery_report_groups_outcomes_and_errors(workspace, owner, fake_email):
    contact = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "a@x.test"}
    )
    reply(workspace, contact, Actor.human(owner.pk), idempotency_key="one")
    gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=contact.pk,
        channel="sms",
        body="blocked",
    )
    report = {r["channel"]: r for r in queries.delivery(workspace.pk, *window())}
    assert (
        report["email"]["statuses"].get("sent", 0) + report["email"]["statuses"].get("pending", 0)
        == 1
    )
    assert report["sms"]["statuses"] == {"blocked": 1} and report["sms"]["delivery_rate"] is None
    assert report["sms"]["errors"][0]["count"] == 1


def test_pipeline_report_counts_open_value_wins_and_losses(workspace):
    contact = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "a@x.test"}
    )
    flow = pipelines.create_pipeline(
        workspace.pk, name="Sales", stages=[("New", 10), ("Won it", 90)]
    )
    open_deal = pipelines.create_opportunity(
        workspace.pk,
        SYSTEM,
        contact_id=contact.pk,
        pipeline_id=flow.pk,
        name="A",
        value_micros=1000,
    )
    won = pipelines.create_opportunity(
        workspace.pk,
        SYSTEM,
        contact_id=contact.pk,
        pipeline_id=flow.pk,
        name="B",
        value_micros=5000,
    )
    lost = pipelines.create_opportunity(
        workspace.pk, SYSTEM, contact_id=contact.pk, pipeline_id=flow.pk, name="C", value_micros=300
    )
    pipelines.win(workspace.pk, SYSTEM, won)
    pipelines.lose(workspace.pk, SYSTEM, lost)
    (report,) = queries.pipeline(workspace.pk, *window())
    new = report["stages"][0]
    assert (new["open_count"], new["open_value_micros"]) == (1, open_deal.value_micros)
    assert (report["won"], report["won_value_micros"], report["lost"]) == (1, 5000, 1)
    assert report["avg_days_to_win"] is not None


def test_attribution_credits_won_value_to_the_source(workspace):
    ad = contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"email": "a@x.test", "source": "meta_lead_ad", "attribution": {"campaign_id": "CP1"}},
    )
    contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "b@x.test", "source": "web"}
    )
    flow = pipelines.create_pipeline(workspace.pk, name="Sales", stages=[("New", 10)])
    deal = pipelines.create_opportunity(
        workspace.pk, SYSTEM, contact_id=ad.pk, pipeline_id=flow.pk, name="D", value_micros=7000
    )
    pipelines.win(workspace.pk, SYSTEM, deal)
    report = queries.attribution(workspace.pk, *window())
    by_source = {s["source"]: s for s in report["sources"]}
    assert by_source["meta_lead_ad"]["won_value_micros"] == 7000
    assert by_source["web"]["won_value_micros"] == 0
    assert report["campaigns"] == [{"campaign_id": "CP1", "contacts": 1}]


def test_agent_report_combines_messages_tasks_and_deals(workspace, owner, fake_email):
    contact = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "a@x.test", "assigned_to": owner.pk}
    )
    reply(workspace, contact, Actor.human(owner.pk))
    task = activity.create_task(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=contact.pk,
        title="Call",
        assigned_to=owner.pk,
    )
    activity.complete_task(workspace_id=workspace.pk, actor=Actor.human(owner.pk), task_id=task.pk)
    flow = pipelines.create_pipeline(workspace.pk, name="Sales", stages=[("New", 10)])
    deal = pipelines.create_opportunity(
        workspace.pk,
        SYSTEM,
        contact_id=contact.pk,
        pipeline_id=flow.pk,
        name="D",
        value_micros=900,
        assigned_to=owner.pk,
    )
    pipelines.win(workspace.pk, SYSTEM, deal)
    (row,) = queries.agents(workspace.pk, *window())
    assert row["user_id"] == str(owner.pk)
    assert (
        row["messages_sent"],
        row["tasks_completed"],
        row["deals_won"],
        row["won_value_micros"],
    ) == (1, 1, 1, 900)


def test_periods_are_validated(workspace):
    from core.errors import ValidationFailedError

    now = timezone.now()
    with pytest.raises(ValidationFailedError):
        queries.period(now, now - timedelta(days=1))
    with pytest.raises(ValidationFailedError):
        queries.period(now - timedelta(days=4000), now)
    start, end = queries.period(None, None)
    assert (end - start).days == 30


def api(client, path, workspace, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(
        f"/api/v1/crm/reports/{path}" + (f"?{query}" if query else ""),
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )


def logged(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.parametrize("path", ["response-time", "delivery", "pipeline", "attribution", "agents"])
def test_every_report_needs_the_permission_and_stays_in_its_workspace(workspace, owner, path):
    assert api(logged(owner), path, workspace).status_code == 200
    viewer = logged(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, path, workspace).status_code == 403
    outsider = logged(make_user("o@example.com"))
    assert api(outsider, path, workspace).status_code in (403, 404, 422)
    assert api(Client(), path, workspace).status_code in (401, 403)
    assert (
        api(
            logged(owner),
            path,
            workspace,
            since="2026-01-02T00:00:00Z",
            until="2026-01-01T00:00:00Z",
        ).status_code
        == 422
    )


def test_reports_never_mix_workspaces(workspace, owner, fake_email):
    result = inbound(workspace)
    reply(workspace, result.contact, Actor.human(owner.pk))
    dispatch_batch()
    other = create_workspace(name="Other Co", owner=owner)
    mine = json.loads(api(logged(owner), "response-time", workspace).content)
    theirs = json.loads(api(logged(owner), "response-time", other).content)
    assert mine["count"] == 1 and theirs["count"] == 0
    assert api(logged(owner), "delivery", other).json() == []
