from datetime import timedelta

import pytest
from django.utils import timezone

from audit.actors import Actor
from core.errors import ValidationFailedError
from crm.campaigns import services
from crm.campaigns.models import Campaign, CampaignRecipient
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.sending import registry
from workspaces.models import Membership

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
BODY = "Version A for {{contact.first_name}}. {{system.unsubscribe_url}}"
VARIANT = "Version B for {{contact.first_name}}. {{system.unsubscribe_url}}"


@pytest.fixture
def mail():
    fake = FakeAdapter("email")
    registry.register("email", lambda: fake)
    yield fake
    from crm.channels.email_smtp import SmtpEmailAdapter

    registry.register("email", SmtpEmailAdapter)


@pytest.fixture(autouse=True)
def _commit_hooks(monkeypatch):
    from django.db import transaction

    monkeypatch.setattr(transaction, "on_commit", lambda func, *a, **k: func())


@pytest.fixture
def membership(workspace, owner):
    return Membership.objects.get(workspace=workspace, user=owner)


def audience(workspace, n):
    for i in range(n):
        c = contacts.create_contact(
            workspace_id=workspace.pk,
            actor=SYSTEM,
            data={"first_name": f"P{i}", "email": f"p{i}@example.com"},
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


def make(membership, **extra):
    values = {"name": "Test", "channel": "email", "subject": "Hello", "body": BODY, **extra}
    return services.create(membership=membership, actor=SYSTEM, **values)


def run_all(campaign, workspace):
    for _ in range(40):
        campaign.refresh_from_db()
        if campaign.status != "sending":
            break
        Campaign.objects.filter(pk=campaign.pk).update(
            next_batch_at=timezone.now() - timedelta(seconds=1)
        )
        services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    campaign.refresh_from_db()
    return campaign


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"variant_percent": 51, "variant_subject": "x"}, "variant_percent"),
        ({"variant_percent": 20}, "variant_subject"),  # a test needs something to compare
        ({"variant_percent": 20, "variant_body": "No way out"}, "variant_body"),
        ({"variant_percent": 20, "variant_subject": "Hi {{nope.field}}"}, None),
        ({"channel": "sms", "body": "hi", "track_opens": True}, "track_opens"),
    ],
)
def test_bad_tests_are_refused(membership, extra, field):
    with pytest.raises(ValidationFailedError) as caught:
        make(membership, **extra)
    if field:
        assert field in caught.value.params["errors"]


def test_the_split_follows_the_percentage_and_never_changes(workspace, membership, mail):
    audience(workspace, 60)
    campaign = make(
        membership, variant_percent=30, variant_subject="Subject B", variant_body=VARIANT
    )
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    split = dict(CampaignRecipient.objects.values_list("contact_id", "variant"))
    share = sum(1 for v in split.values() if v == "B") / len(split)
    assert 0.1 <= share <= 0.5  # about 30% of 60, by chance within reason
    again = [services._variant_of(campaign, contact_id) for contact_id in split]
    assert again == list(split.values())


def test_each_group_gets_its_own_words(workspace, membership, mail):
    audience(workspace, 20)
    campaign = make(
        membership, variant_percent=50, variant_subject="Subject B", variant_body=VARIANT
    )
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    run_all(campaign, workspace)
    subjects = {r.subject for r in mail.sent}
    assert subjects == {"Hello", "Subject B"}
    assert all(("Version B" in r.body) == (r.subject == "Subject B") for r in mail.sent)


def test_a_variant_with_only_a_new_subject_keeps_the_body(workspace, membership, mail):
    audience(workspace, 10)
    campaign = make(membership, variant_percent=50, variant_subject="Subject B")
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    run_all(campaign, workspace)
    assert all("Version A" in r.body for r in mail.sent)


def test_stats_are_split_by_variant(workspace, membership, mail):
    audience(workspace, 20)
    campaign = make(membership, variant_percent=50, variant_subject="Subject B")
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    run_all(campaign, workspace)
    rows = {r["variant"]: r for r in services.stats(workspace.pk, campaign.pk)["variants"]}
    assert set(rows) == {"A", "B"}
    assert rows["A"]["sent"] + rows["B"]["sent"] == 20
    assert rows["A"]["recipients"] + rows["B"]["recipients"] == 20


def test_without_a_test_only_one_group_is_reported(workspace, membership, mail):
    audience(workspace, 3)
    campaign = make(membership)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    run_all(campaign, workspace)
    assert [r["variant"] for r in services.stats(workspace.pk, campaign.pk)["variants"]] == ["A"]


def test_drafts_can_be_changed_into_a_test(workspace, membership):
    campaign = make(membership)
    updated = services.update(
        membership=membership,
        actor=SYSTEM,
        campaign_id=campaign.pk,
        data={"variant_percent": 25, "variant_subject": "B", "track_opens": True},
    )
    assert (updated.variant_percent, updated.track_opens) == (25, True)
