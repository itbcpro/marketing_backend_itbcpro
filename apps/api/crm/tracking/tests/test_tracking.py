import re
import uuid
from datetime import timedelta

import pytest
from django.core import signing
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from crm.campaigns import services
from crm.campaigns.models import Campaign, CampaignRecipient
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.sending import registry
from crm.tracking import services as tracking
from crm.tracking.models import EmailEvent
from workspaces.models import Membership

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
BODY = "Hi {{contact.first_name}}! Read https://shop.example.com/sale?a=1&b=2. Bye {{system.unsubscribe_url}}"
WS, MSG, CONTACT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


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


def audience(workspace, n):
    people = []
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
        people.append(c)
    return people


def run_all(campaign, workspace):
    for _ in range(30):
        campaign.refresh_from_db()
        if campaign.status != "sending":
            break
        Campaign.objects.filter(pk=campaign.pk).update(
            next_batch_at=timezone.now() - timedelta(seconds=1)
        )
        services.process_batch(workspace_id=workspace.pk, campaign_id=campaign.pk)
    campaign.refresh_from_db()
    return campaign


def launch(workspace, owner, **extra):
    membership = Membership.objects.get(workspace=workspace, user=owner)
    values = {"name": "Spring", "channel": "email", "subject": "Hello", "body": BODY, **extra}
    campaign = services.create(membership=membership, actor=SYSTEM, **values)
    services.launch(workspace_id=workspace.pk, actor=SYSTEM, campaign_id=campaign.pk)
    return run_all(campaign, workspace)


# ---- decorating -------------------------------------------------------------------------------


def test_links_are_wrapped_the_way_out_is_not_and_punctuation_stays_outside():
    text = "See https://shop.example.com/sale?a=1&b=2. Leave: https://x.test/api/v1/crm-public/unsubscribe/abc"
    out = tracking.decorate(WS, MSG, CONTACT, text, opens=True, clicks=True)
    assert "https://shop.example.com" not in out.text
    assert out.text.count("/crm-public/t/c/") == 1
    assert "https://x.test/api/v1/crm-public/unsubscribe/abc" in out.text  # untouched
    assert re.search(r"/t/c/[\w:\-]+\. Leave", out.text)  # the full stop stays outside the link
    assert "/crm-public/t/o/" in out.html and 'width="1"' in out.html
    assert "&amp;b=2" not in out.html.split("/t/c/")[0]  # nothing broken before the link


def test_html_escapes_the_message_text():
    out = tracking.decorate(
        WS, MSG, CONTACT, "<script>alert(1)</script> & more", opens=False, clicks=False
    )
    assert "<script>" not in out.html and "&lt;script&gt;" in out.html
    assert "/t/o/" not in out.html  # no pixel unless opens are tracked


def test_clicks_off_leaves_links_alone_but_opens_still_add_the_pixel():
    out = tracking.decorate(WS, MSG, CONTACT, "https://a.test/x", opens=True, clicks=False)
    assert out.text == "https://a.test/x" and "/t/o/" in out.html


# ---- recording --------------------------------------------------------------------------------


def token_for(workspace, url=""):
    return tracking._token(workspace.pk, MSG, CONTACT, url)


def test_the_pixel_always_answers_with_an_image_and_counts_only_the_first_open(workspace):
    client = Client()
    token = token_for(workspace)
    for _ in range(3):
        res = client.get(f"/api/v1/crm-public/t/o/{token}")
        assert res.status_code == 200 and res["Content-Type"] == "image/gif"
        assert res.content.startswith(b"GIF89a") and "no-store" in res["Cache-Control"]
    assert EmailEvent.objects.filter(kind="open").count() == 1
    broken = client.get("/api/v1/crm-public/t/o/garbage")
    assert broken.status_code == 200 and broken["Content-Type"] == "image/gif"
    assert EmailEvent.objects.count() == 1


def test_a_click_redirects_and_is_counted_every_time(workspace):
    client = Client()
    token = token_for(workspace, "https://shop.example.com/sale?a=1")
    for _ in range(2):
        res = client.get(f"/api/v1/crm-public/t/c/{token}")
        assert res.status_code == 302 and res["Location"] == "https://shop.example.com/sale?a=1"
        assert res["Referrer-Policy"] == "no-referrer"
    assert EmailEvent.objects.filter(kind="click").count() == 2


@pytest.mark.parametrize("bad", ["garbage", "", "a:b:c"])
def test_tampered_links_go_nowhere(workspace, bad):
    assert Client().get(f"/api/v1/crm-public/t/c/{bad or 'x'}").status_code == 404
    assert EmailEvent.objects.count() == 0


def test_only_web_addresses_can_be_redirect_targets(workspace):
    for target in (
        "javascript:alert(1)",
        "file:///etc/passwd",
        "//evil.test/x",
        "data:text/html,x",
    ):
        token = signing.dumps(
            {"w": str(workspace.pk), "m": str(MSG), "c": str(CONTACT), "u": target},
            salt=tracking.SALT,
        )
        assert Client().get(f"/api/v1/crm-public/t/c/{token}").status_code == 404


def test_old_links_stop_working(workspace, settings):
    token = token_for(workspace, "https://a.test/")
    settings.CRM_TRACKING_MAX_AGE_DAYS = 0
    assert Client().get(f"/api/v1/crm-public/t/c/{token}").status_code == 404


# ---- through a campaign -----------------------------------------------------------------------


def test_a_tracked_campaign_sends_wrapped_links_and_an_html_part(workspace, owner, mail):
    audience(workspace, 1)
    launch(workspace, owner, track_opens=True, track_clicks=True)
    request = mail.sent[0]
    assert "/crm-public/t/c/" in request.body and "shop.example.com" not in request.body
    assert "/unsubscribe/" in request.body  # the way out is intact
    assert "/crm-public/t/o/" in request.html
    assert request.html.count("<a href=") == 2  # the sale link and the unsubscribe link


def test_an_untracked_campaign_is_sent_exactly_as_written(workspace, owner, mail):
    audience(workspace, 1)
    launch(workspace, owner)
    request = mail.sent[0]
    assert "https://shop.example.com/sale?a=1&b=2." in request.body and request.html == ""


def test_opens_and_clicks_reach_the_campaign_stats(workspace, owner, mail):
    audience(workspace, 3)
    campaign = launch(workspace, owner, track_opens=True, track_clicks=True)
    client = Client()
    for request in mail.sent[:2]:
        pixel = request.html.split("/t/o/")[1].split('"')[0]
        client.get(f"/api/v1/crm-public/t/o/{pixel}")
    link = mail.sent[0].body.split("/t/c/")[1].split()[0].rstrip(".")
    client.get(f"/api/v1/crm-public/t/c/{link}")
    row = services.stats(workspace.pk, campaign.pk)["variants"][0]
    assert (row["sent"], row["opened"], row["clicked"]) == (3, 2, 1)
    assert CampaignRecipient.objects.count() == 3


def test_erasing_a_person_removes_what_was_recorded_about_their_reading(workspace, owner, mail):
    from audit.outbox import dispatch_batch

    (person,) = audience(workspace, 1)
    launch(workspace, owner, track_opens=True)
    pixel = mail.sent[0].html.split("/t/o/")[1].split('"')[0]
    Client().get(f"/api/v1/crm-public/t/o/{pixel}")
    assert EmailEvent.objects.count() == 1
    contacts.erase_contact(workspace_id=workspace.pk, actor=SYSTEM, contact_id=person.pk)
    dispatch_batch()
    assert EmailEvent.objects.count() == 0
