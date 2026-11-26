import json
from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from conftest import make_user
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.campaigns import services, tasks
from crm.campaigns.models import Campaign, CampaignRecipient
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox.models import Message
from crm.sending import registry
from crm.templates import services as templates
from workspaces.models import Membership, RecordScope, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
BODY = "Hi {{contact.first_name}}, news! Unsubscribe: {{system.unsubscribe_url}}"


@pytest.fixture
def mail():
    fake = FakeAdapter("email")
    registry.register("email", lambda: fake)
    yield fake
    from crm.channels.email_smtp import SmtpEmailAdapter

    registry.register("email", SmtpEmailAdapter)


@pytest.fixture
def membership(workspace, owner):
    return Membership.objects.get(workspace=workspace, user=owner)


def audience(workspace, n=4, opted_in=True):
    out = []
    for i in range(n):
        c = contacts.create_contact(
            workspace_id=workspace.pk,
            actor=SYSTEM,
            data={
                "first_name": f"P{i}",
                "email": f"p{i}@example.com",
                "tags": ["spring"] if i % 2 == 0 else [],
            },
        )
        if opted_in:
            consent.record(
                workspace_id=workspace.pk,
                actor=SYSTEM,
                contact_id=c.pk,
                channel="email",
                purpose="marketing",
                status="opted_in",
                source="form",
            )
        out.append(c)
    return out


def make(membership, **extra):
    values = {"name": "Spring", "channel": "email", "subject": "Hello", "body": BODY, **extra}
    return services.create(membership=membership, actor=SYSTEM, **values)


def run_all(campaign, workspace):
    """Run batches until the campaign stops asking for more."""
    for _ in range(20):
        campaign.refresh_from_db()
        if campaign.status != "sending":
            break
        Campaign.objects.filter(pk=campaign.pk).update(
            next_batch_at=timezone.now() - timedelta(seconds=1)
        )
        services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    campaign.refresh_from_db()
    return campaign


@pytest.fixture(autouse=True)
def _commit_hooks(monkeypatch):
    from django.db import transaction

    monkeypatch.setattr(transaction, "on_commit", lambda func, *a, **k: func())


# ---- drafting ---------------------------------------------------------------------------------


def test_a_marketing_email_must_carry_the_unsubscribe_link(membership):
    with pytest.raises(ValidationFailedError) as caught:
        make(membership, body="Buy now")
    assert caught.value.params["errors"] == {"body": "unsubscribe_required"}
    assert make(membership, body="Your receipt", purpose="transactional").pk
    assert make(
        membership, name="SMS", channel="sms", body="Sale today"
    ).pk  # sms has its own STOP rule


@pytest.mark.parametrize(
    "bad",
    [
        {"name": " "},
        {"channel": "pigeon"},
        {"purpose": "spam"},
        {"body": " "},
        {"batch_size": 0},
        {"batch_size": 5000},
        {"batch_interval_seconds": 1},
        {"body": "{{contact.nope}} {{system.unsubscribe_url}}"},
        {"audience": {"field": "password", "operator": "eq", "value": 1}},
    ],
)
def test_invalid_drafts_are_rejected(membership, bad):
    with pytest.raises(ValidationFailedError):
        make(membership, **bad)


def test_a_template_fills_in_the_message_and_defaults_come_from_settings(
    workspace, membership, settings
):
    settings.CRM_CAMPAIGN_BATCH_SIZE, settings.CRM_CAMPAIGN_BATCH_INTERVAL = 7, 90
    template = templates.create(
        workspace_id=workspace.pk, actor=SYSTEM, name="T", subject="Subj", body=BODY
    )
    campaign = services.create(
        membership=membership,
        actor=SYSTEM,
        name="From template",
        channel="email",
        template_id=template.pk,
    )
    assert (
        campaign.subject,
        campaign.body,
        campaign.batch_size,
        campaign.batch_interval_seconds,
    ) == ("Subj", BODY, 7, 90)


def test_only_drafts_can_be_edited(membership, workspace, mail):
    campaign = make(membership)
    updated = services.update(
        membership=membership, actor=SYSTEM, campaign_id=campaign.pk, data={"subject": "New"}
    )
    assert updated.subject == "New"
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    with pytest.raises(ConflictError):
        services.update(
            membership=membership, actor=SYSTEM, campaign_id=campaign.pk, data={"subject": "Late"}
        )


# ---- sending ----------------------------------------------------------------------------------


def test_a_campaign_sends_in_batches_personalised_and_finishes(workspace, membership, mail):
    audience(workspace, 5)
    campaign = make(membership, batch_size=2)
    launched = services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    assert (
        launched.status == "sending"
        and CampaignRecipient.objects.filter(campaign=campaign).count() == 5
    )
    services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    assert len(mail.sent) == 2  # one batch only
    Campaign.objects.filter(pk=campaign.pk).update(next_batch_at=timezone.now())
    done = run_all(campaign, workspace)
    assert done.status == "done" and done.finished_at and len(mail.sent) == 5
    first = mail.sent[0]
    assert (
        first.body.startswith("Hi P") and "/unsubscribe/" in first.body and first.subject == "Hello"
    )
    assert services.stats(workspace.pk, campaign.pk)["recipients"] == {
        "pending": 0,
        "sent": 5,
        "blocked": 0,
        "skipped": 0,
    }


def test_a_batch_that_is_not_due_does_nothing(workspace, membership, mail):
    audience(workspace, 3)
    campaign = make(membership, batch_size=1)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    assert (
        services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk) is None
    )  # lease holds
    assert len(mail.sent) == 1


def test_people_without_consent_are_blocked_with_a_reason_and_never_mailed(
    workspace, membership, mail
):
    audience(workspace, 2, opted_in=True)
    stranger = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "no@example.com"}
    )
    gone = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"phone": "416-555-0100"}
    )
    campaign = make(membership)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    run_all(campaign, workspace)
    stats = services.stats(workspace.pk, campaign.pk)
    assert stats["recipients"]["sent"] == 2 and stats["recipients"]["blocked"] == 2
    assert stats["blocked_reasons"] == {"no_consent": 1, "no_address": 1}
    assert len(mail.sent) == 2
    blocked = CampaignRecipient.objects.get(campaign=campaign, contact=stranger)
    assert Message.objects.get(pk=blocked.message_id).status == "blocked"
    assert CampaignRecipient.objects.get(campaign=campaign, contact=gone).reason == "no_address"


def test_an_audience_filter_selects_who_gets_it(workspace, membership, mail):
    people = audience(workspace, 4)
    campaign = make(membership, audience={"field": "tags", "operator": "has", "value": "spring"})
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    run_all(campaign, workspace)
    sent_to = {r.to for r in mail.sent}
    assert sent_to == {people[0].email, people[2].email}


def test_the_creators_record_scope_limits_the_audience(workspace, owner, mail):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    Membership.objects.filter(user=rep).update(record_scope=RecordScope.ASSIGNED)
    people = audience(workspace, 3)
    contacts.assign(
        workspace_id=workspace.pk, actor=SYSTEM, contact_id=people[1].pk, user_id=rep.pk
    )
    rep_membership = Membership.objects.get(workspace=workspace, user=rep)
    campaign = make(rep_membership)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    run_all(campaign, workspace)
    assert [r.to for r in mail.sent] == [people[1].email]


def test_quiet_hours_postpone_instead_of_dropping(workspace, membership, monkeypatch):
    sms = FakeAdapter("sms")
    registry.register("sms", lambda: sms)
    c = contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"phone": "416-555-0199", "time_zone": "America/Toronto"},
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
    campaign = make(membership, channel="sms", body="Sale today", subject="")
    night = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: night)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    recipient = CampaignRecipient.objects.get(campaign=campaign)
    assert recipient.status == "pending" and sms.sent == [] and not Message.objects.exists()
    Campaign.objects.filter(pk=campaign.pk).update(next_batch_at=night)
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 11, 19, 15, 0, tzinfo=UTC))
    services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    recipient.refresh_from_db()
    assert recipient.status == "sent" and len(sms.sent) == 1
    registry.unregister("sms")


def test_running_the_same_batch_twice_never_double_sends(workspace, membership, mail):
    audience(workspace, 3)
    campaign = make(membership)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    CampaignRecipient.objects.filter(campaign=campaign).update(
        status="pending"
    )  # simulate a lost write
    Campaign.objects.filter(pk=campaign.pk).update(status="sending", next_batch_at=timezone.now())
    services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    assert len(mail.sent) == 3 and Message.objects.count() == 3  # idempotency keys held


def test_erased_contacts_are_skipped(workspace, membership, mail):
    people = audience(workspace, 2)
    campaign = make(membership)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    contacts.delete_contact(workspace_id=workspace.pk, actor=SYSTEM, contact_id=people[0].pk)
    run_all(campaign, workspace)
    assert (
        services.stats(workspace.pk, campaign.pk)["recipients"]["skipped"] == 1
        and len(mail.sent) == 1
    )


def test_an_empty_audience_finishes_at_once(workspace, membership, mail):
    campaign = make(membership)
    assert (
        services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk).status
        == "done"
    )


# ---- control ----------------------------------------------------------------------------------


def test_pause_resume_and_cancel(workspace, membership, mail):
    audience(workspace, 4)
    campaign = make(membership, batch_size=1)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    assert services.pause(workspace_id=workspace.pk, campaign_id=campaign.pk).status == "paused"
    assert services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk) is None
    with pytest.raises(ConflictError):
        services.pause(workspace_id=workspace.pk, campaign_id=campaign.pk)
    assert services.resume(workspace_id=workspace.pk, campaign_id=campaign.pk).status == "sending"
    canceled = services.cancel(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    assert canceled.status == "canceled"
    stats = services.stats(workspace.pk, campaign.pk)["recipients"]
    assert stats["sent"] == 1 and stats["skipped"] == 3 and stats["pending"] == 0
    with pytest.raises(ConflictError):
        services.cancel(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    assert len(mail.sent) == 1


def test_scheduled_campaigns_start_when_due(workspace, membership, mail):
    audience(workspace, 2)
    campaign = make(membership)
    soon = timezone.now() + timedelta(hours=1)
    assert (
        services.launch(
            workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk, scheduled_at=soon
        ).status
        == "scheduled"
    )
    assert tasks.tick.apply().get() == 0
    Campaign.objects.filter(pk=campaign.pk).update(
        scheduled_at=timezone.now() - timedelta(seconds=1)
    )
    assert tasks.tick.apply().get() == 1  # the start
    campaign.refresh_from_db()
    assert campaign.status == "sending" and mail.sent == []
    assert tasks.tick.apply().get() == 1  # the first batch
    campaign.refresh_from_db()
    assert campaign.status == "done" and len(mail.sent) == 2


def test_the_tick_job_drives_a_campaign_to_the_end(workspace, membership, mail):
    audience(workspace, 3)
    campaign = make(membership, batch_size=2)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    for _ in range(4):
        Campaign.objects.filter(pk=campaign.pk).update(
            next_batch_at=timezone.now() - timedelta(seconds=1)
        )
        tasks.tick.apply().get()
    campaign.refresh_from_db()
    assert campaign.status == "done" and len(mail.sent) == 3


def test_a_scheduled_campaign_whose_owner_left_is_canceled(workspace, membership, mail, owner):
    audience(workspace, 1)
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    campaign = make(Membership.objects.get(workspace=workspace, user=rep))
    services.launch(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        campaign_id=campaign.pk,
        scheduled_at=timezone.now() + timedelta(hours=1),
    )
    Membership.objects.filter(user=rep).delete()
    Campaign.objects.filter(pk=campaign.pk).update(
        scheduled_at=timezone.now() - timedelta(seconds=1)
    )
    assert (
        services.start_scheduled(workspace_id=workspace.pk, campaign_id=campaign.pk).status
        == "canceled"
    )
    assert mail.sent == []


# ---- API --------------------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def test_api_lifecycle(workspace, owner, mail):
    audience(workspace, 3)
    admin = Client()
    admin.force_login(owner)
    created = api(
        admin,
        "post",
        "/campaigns",
        workspace,
        {"name": "API", "channel": "email", "subject": "S", "body": BODY, "batch_size": 10},
    )
    assert created.status_code == 201
    cid = created.json()["id"]
    assert (
        api(admin, "patch", f"/campaigns/{cid}", workspace, {"subject": "S2"}).json()["subject"]
        == "S2"
    )
    launched = api(admin, "post", f"/campaigns/{cid}/launch", workspace, {})
    assert launched.json()["status"] == "sending"
    assert api(admin, "post", f"/campaigns/{cid}/launch", workspace, {}).status_code == 409
    services.process_batch(workspace_id=workspace.pk, campaign_id=__import__("uuid").UUID(cid))
    stats = api(admin, "get", f"/campaigns/{cid}/stats", workspace).json()
    assert stats["recipients"]["sent"] == 3 and stats["delivery"]
    page = api(admin, "get", f"/campaigns/{cid}/recipients?status=sent&limit=2", workspace).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    assert api(admin, "get", "/campaigns", workspace).json()["items"][0]["id"] == cid
    assert (
        api(admin, "post", f"/campaigns/{cid}/cancel", workspace).status_code == 409
    )  # already done
    draft = api(
        admin, "post", "/campaigns", workspace, {"name": "Draft", "channel": "email", "body": BODY}
    ).json()["id"]
    assert (
        api(admin, "post", f"/campaigns/{draft}/cancel", workspace).json()["status"] == "canceled"
    )
    bad = api(
        admin,
        "post",
        "/campaigns",
        workspace,
        {"name": "Bad", "channel": "email", "body": "no link"},
    )
    assert bad.status_code == 422 and bad.json()["errors"] == [
        {"loc": ["body"], "type": "unsubscribe_required"}
    ]


def test_api_permissions_and_tenants(workspace, owner, mail):
    audience(workspace, 1)
    admin, viewer = Client(), Client()
    admin.force_login(owner)
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    body = {"name": "X", "channel": "email", "body": BODY}
    assert api(viewer, "post", "/campaigns", workspace, body).status_code == 403
    cid = api(admin, "post", "/campaigns", workspace, body).json()["id"]
    assert api(viewer, "get", f"/campaigns/{cid}", workspace).status_code == 200
    assert (
        api(viewer, "get", f"/campaigns/{cid}/stats", workspace).status_code == 403
    )  # analysts and above
    assert api(viewer, "post", f"/campaigns/{cid}/launch", workspace, {}).status_code == 403
    other = create_workspace(name="Other Co", owner=owner)
    assert api(admin, "get", "/campaigns", other).json()["items"] == []
    for method, path, payload in (
        ("get", f"/campaigns/{cid}", None),
        ("patch", f"/campaigns/{cid}", {"name": "Z"}),
        ("post", f"/campaigns/{cid}/launch", {}),
        ("post", f"/campaigns/{cid}/pause", None),
        ("post", f"/campaigns/{cid}/cancel", None),
        ("get", f"/campaigns/{cid}/stats", None),
        ("get", f"/campaigns/{cid}/recipients", None),
    ):
        assert api(admin, method, path, other, payload).status_code == 404, path
    with pytest.raises(NotFoundError):
        services.get(other.pk, __import__("uuid").UUID(cid))
