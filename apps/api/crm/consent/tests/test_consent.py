import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client

from audit.actors import Actor
from audit.models import OutboxMessage
from audit.outbox import dispatch_batch
from conftest import make_user
from core.errors import ValidationFailedError
from crm.consent import services
from crm.consent.keywords import KeywordAction, classify
from crm.consent.models import ConsentEvent, ConsentState
from crm.contacts import services as contacts
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
ACTOR = Actor.system("test")
NOON = datetime(2026, 11, 18, 17, 0, tzinfo=UTC)  # 12:00 in Toronto
NIGHT = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)  # 22:00 in Toronto


def contact(workspace, **data):
    data.setdefault("time_zone", "America/Toronto")
    return contacts.create_contact(workspace_id=workspace.pk, actor=ACTOR, data=data)


def grant(workspace, c, channel="email", purpose="marketing", status="opted_in", **extra):
    return services.record(
        workspace_id=workspace.pk,
        actor=ACTOR,
        contact_id=c.pk,
        channel=channel,
        purpose=purpose,
        status=status,
        source=extra.pop("source", "form"),
        **extra,
    )


def decide(workspace, c, channel="email", purpose="marketing", now=NOON):
    return services.decide(
        workspace_id=workspace.pk, contact_id=c.pk, channel=channel, purpose=purpose, now=now
    )


# ---- ledger ------------------------------------------------------------------------------------


def test_every_record_appends_and_state_follows_the_latest(workspace):
    c = contact(workspace, email="a@example.com")
    grant(workspace, c, status="opted_in", evidence={"form": "https://x.test/f"})
    grant(workspace, c, status="opted_out", source="link")
    grant(workspace, c, status="opted_in")
    assert ConsentEvent.objects.count() == 3
    state = ConsentState.objects.get(contact=c, channel="email", purpose="marketing")
    assert state.status == "opted_in"
    assert [e.status for e in services.history(workspace.pk, c.pk)] == [
        "opted_in",
        "opted_out",
        "opted_in",
    ]


def test_events_cannot_be_edited(workspace):
    c = contact(workspace)
    entry = grant(workspace, c)
    entry.status = "opted_out"
    with pytest.raises(RuntimeError):
        entry.save()


def test_only_real_changes_publish_an_event(workspace):
    c = contact(workspace)
    grant(workspace, c)
    OutboxMessage.objects.all().delete()
    grant(workspace, c)  # same status again: ledger row, no change event
    assert not OutboxMessage.objects.filter(topic="crm.consent.changed").exists()
    grant(workspace, c, status="opted_out")
    assert OutboxMessage.objects.filter(topic="crm.consent.changed").count() == 1


def test_invalid_values_are_rejected_together(workspace):
    c = contact(workspace)
    with pytest.raises(ValidationFailedError) as caught:
        services.record(
            workspace_id=workspace.pk,
            actor=ACTOR,
            contact_id=c.pk,
            channel="pigeon",
            purpose="marketing",
            status="maybe",
            source="form",
        )
    assert set(caught.value.params["errors"]) == {"channel", "status"}


def test_evidence_export_is_chronological_and_complete(workspace):
    c = contact(workspace)
    grant(workspace, c, evidence={"ip": "203.0.113.5"})
    grant(workspace, c, status="opted_out", source="link")
    rows = services.evidence_export(workspace.pk, c.pk)
    assert [r["status"] for r in rows] == ["opted_in", "opted_out"]
    assert rows[0]["evidence"] == {"ip": "203.0.113.5"} and rows[0]["actor"] == "system:test"


# ---- decisions -----------------------------------------------------------------------------------


def test_marketing_needs_opt_in_under_the_default_canadian_profile(workspace):
    c = contact(workspace)
    assert decide(workspace, c).reason == "no_consent"
    grant(workspace, c)
    assert decide(workspace, c).allowed


def test_opt_out_blocks_and_is_per_channel(workspace):
    c = contact(workspace)
    grant(workspace, c, channel="email")
    grant(workspace, c, channel="sms")
    grant(workspace, c, channel="sms", status="opted_out", source="keyword")
    assert decide(workspace, c, channel="sms").reason == "opted_out"
    assert decide(workspace, c, channel="email").allowed


def test_transactional_only_needs_the_absence_of_an_opt_out(workspace):
    c = contact(workspace)
    assert decide(workspace, c, purpose="transactional").allowed
    grant(workspace, c, purpose="transactional", status="opted_out")
    assert decide(workspace, c, purpose="transactional").reason == "opted_out"
    grant(workspace, c, purpose="marketing")
    assert decide(workspace, c, purpose="marketing").allowed  # purposes are independent


def test_implied_consent_expires(workspace):
    c = contact(workspace)
    grant(workspace, c, source="import", expires_at=NOON + timedelta(days=1))
    assert decide(workspace, c).allowed
    assert decide(workspace, c, now=NOON + timedelta(days=2)).reason == "consent_expired"


def test_quiet_hours_apply_to_marketing_sms_in_the_contacts_time_zone(workspace):
    c = contact(workspace)
    grant(workspace, c, channel="sms")
    assert decide(workspace, c, channel="sms", now=NOON).allowed
    assert decide(workspace, c, channel="sms", now=NIGHT).reason == "quiet_hours"
    assert decide(workspace, c, channel="sms", purpose="transactional", now=NIGHT).allowed
    grant(workspace, c, channel="email")
    assert decide(workspace, c, channel="email", now=NIGHT).allowed  # email has no quiet hours
    services.set_law_profile(workspace_id=workspace.pk, profile="ca", quiet_hours=False)
    assert decide(workspace, c, channel="sms", now=NIGHT).allowed


def test_quiet_hours_fall_back_to_the_workspace_time_zone(workspace):
    c = contacts.create_contact(workspace_id=workspace.pk, actor=ACTOR, data={})
    grant(workspace, c, channel="sms")
    workspace.time_zone = "America/Toronto"
    workspace.save()
    assert decide(workspace, c, channel="sms", now=NIGHT).reason == "quiet_hours"


def test_us_profile_does_not_need_opt_in_for_email(workspace):
    services.set_law_profile(workspace_id=workspace.pk, profile="us")
    c = contact(workspace)
    assert decide(workspace, c, channel="email").allowed
    assert decide(workspace, c, channel="sms").reason == "no_consent"


def test_unknown_profile_is_rejected(workspace):
    with pytest.raises(KeyError):
        services.set_law_profile(workspace_id=workspace.pk, profile="atlantis")


# ---- keywords ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("STOP", KeywordAction.OPT_OUT),
        (" stop. ", KeywordAction.OPT_OUT),
        ("Unsubscribe", KeywordAction.OPT_OUT),
        ("ARRÊT", KeywordAction.OPT_OUT),
        ("start", KeywordAction.OPT_IN),
        ("HELP", KeywordAction.HELP),
        ("please stop texting me", None),
        ("", None),
        ("stopping", None),
    ],
)
def test_keyword_classification(text, action):
    assert classify(text) == action


def test_stop_opts_out_of_everything_on_the_channel_and_start_reverses_it(workspace):
    c = contact(workspace)
    grant(workspace, c, channel="sms")
    action = services.handle_keyword(
        workspace_id=workspace.pk, contact_id=c.pk, channel="sms", text="STOP", message_id="m1"
    )
    assert action == KeywordAction.OPT_OUT
    assert decide(workspace, c, channel="sms", purpose="transactional").reason == "opted_out"
    assert decide(workspace, c, channel="sms").reason == "opted_out"
    again = services.handle_keyword(
        workspace_id=workspace.pk, contact_id=c.pk, channel="sms", text="START"
    )
    assert again == KeywordAction.OPT_IN
    assert decide(workspace, c, channel="sms").allowed
    assert (
        services.handle_keyword(
            workspace_id=workspace.pk, contact_id=c.pk, channel="sms", text="hello"
        )
        is None
    )
    assert (
        services.handle_keyword(
            workspace_id=workspace.pk, contact_id=c.pk, channel="sms", text="help"
        )
        == KeywordAction.HELP
    )


# ---- merge ---------------------------------------------------------------------------------------


def test_merge_carries_opt_outs_and_never_loosens(workspace):
    winner = contact(workspace, email="w@example.com")
    loser = contact(workspace, email="l@example.com")
    grant(workspace, winner, channel="email")  # winner opted in
    grant(workspace, loser, channel="email", status="opted_out")  # loser opted out
    grant(workspace, loser, channel="sms")  # loser opted in, winner has no answer
    contacts.merge_contacts(
        workspace_id=workspace.pk, actor=ACTOR, winner_id=winner.pk, loser_id=loser.pk
    )
    assert dispatch_batch() >= 1
    assert decide(workspace, winner, channel="email").reason == "opted_out"
    assert decide(workspace, winner, channel="sms").allowed
    assert ConsentEvent.objects.filter(contact=winner, source="merge").count() == 2


# ---- API ------------------------------------------------------------------------------------------


def api_call(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def test_api_roundtrip_and_permissions(workspace, owner):
    c = contact(workspace)
    admin = Client()
    admin.force_login(owner)
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    body = {
        "channel": "email",
        "purpose": "marketing",
        "status": "opted_in",
        "evidence": {"form": "f1"},
    }
    created = api_call(admin, "post", f"/contacts/{c.pk}/consent", workspace, body)
    assert created.status_code == 201 and created.json()["actor_id"] == str(owner.pk)
    assert (
        api_call(admin, "get", f"/contacts/{c.pk}/consent", workspace).json()[0]["status"]
        == "opted_in"
    )
    assert len(api_call(admin, "get", f"/contacts/{c.pk}/consent/history", workspace).json()) == 1
    check = api_call(
        admin, "get", f"/contacts/{c.pk}/consent/check?channel=email&purpose=marketing", workspace
    )
    assert check.json() == {"allowed": True, "reason": ""}
    assert api_call(viewer, "post", f"/contacts/{c.pk}/consent", workspace, body).status_code == 403
    assert api_call(viewer, "get", f"/contacts/{c.pk}/consent/export", workspace).status_code == 403
    assert len(api_call(admin, "get", f"/contacts/{c.pk}/consent/export", workspace).json()) == 1


def test_api_refuses_system_sources_and_bad_values(workspace, owner):
    c = contact(workspace)
    admin = Client()
    admin.force_login(owner)
    base = {"channel": "email", "purpose": "marketing", "status": "opted_in"}
    assert (
        api_call(
            admin, "post", f"/contacts/{c.pk}/consent", workspace, {**base, "source": "keyword"}
        ).status_code
        == 422
    )
    assert (
        api_call(
            admin, "post", f"/contacts/{c.pk}/consent", workspace, {**base, "channel": "pigeon"}
        ).status_code
        == 422
    )


def test_api_settings_and_cross_tenant(workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    admin = Client()
    admin.force_login(owner)
    assert api_call(admin, "get", "/consent/settings", workspace).json()["law_profile"] == "ca"
    put = api_call(admin, "put", "/consent/settings", workspace, {"law_profile": "us"})
    assert put.json()["law_profile"] == "us" and "eu" in put.json()["available_profiles"]
    assert (
        api_call(admin, "put", "/consent/settings", workspace, {"law_profile": "zz"}).status_code
        == 422
    )
    assert api_call(admin, "get", "/consent/settings", other).json()["law_profile"] == "ca"
    c = contact(workspace)
    for path in ("", "/history", "/export", "/check?channel=sms&purpose=marketing"):
        assert api_call(admin, "get", f"/contacts/{c.pk}/consent{path}", other).status_code == 404
    body = {"channel": "email", "purpose": "marketing", "status": "opted_in"}
    assert api_call(admin, "post", f"/contacts/{c.pk}/consent", other, body).status_code == 404
    assert api_call(admin, "get", f"/contacts/{uuid.uuid4()}/consent", workspace).status_code == 404
