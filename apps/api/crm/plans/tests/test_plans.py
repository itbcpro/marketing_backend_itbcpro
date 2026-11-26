import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import Client

from audit.actors import Actor
from conftest import make_user
from core.errors import ErrorCode
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.plans import services
from crm.plans.models import Plan
from crm.sending import gateway, registry
from crm.workflows import definitions
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


def contact(workspace, n=0):
    return contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": f"p{n}@example.com"}
    )


def test_the_starting_plans_exist_and_free_is_the_default(workspace):
    keys = list(Plan.objects.values_list("key", flat=True))
    assert keys == ["free", "starter", "growth", "scale"]
    assert workspace.organization.plan_key == "free"
    assert (
        services.limit_for(workspace.pk, services.CONTACTS)
        == Plan.objects.get(key="free").limits["contacts"]
    )
    scale = workspace.organization
    services.set_plan(scale, "scale")
    assert services.limit_for(workspace.pk, services.CONTACTS) is None  # no limit written


def test_an_organization_override_beats_its_plan(workspace):
    workspace.organization.limits = {"contacts": 3}
    workspace.organization.save()
    assert services.limit_for(workspace.pk, services.CONTACTS) == 3


def test_contacts_stop_at_the_limit_with_a_clear_error(workspace):
    workspace.organization.limits = {"contacts": 2}
    workspace.organization.save()
    contact(workspace, 1)
    contact(workspace, 2)
    with pytest.raises(services.PlanLimitError) as caught:
        contact(workspace, 3)
    assert caught.value.code == ErrorCode.PLAN_LIMIT and caught.value.http_status == 402
    assert caught.value.params["errors"] == {"contacts": "plan_limit"}


def test_deleting_makes_room_again(workspace):
    workspace.organization.limits = {"contacts": 1}
    workspace.organization.save()
    first = contact(workspace, 1)
    contacts.delete_contact(workspace_id=workspace.pk, actor=SYSTEM, contact_id=first.pk)
    assert contact(workspace, 2).pk


def test_the_limit_reaches_the_api_as_payment_required(workspace, owner):
    workspace.organization.limits = {"contacts": 0}
    workspace.organization.save()
    client = Client()
    client.force_login(owner)
    res = client.post(
        "/api/v1/crm/contacts",
        data=json.dumps({"email": "x@example.com"}),
        content_type="application/json",
        HTTP_X_WORKSPACE_ID=str(workspace.pk),
    )
    assert res.status_code == 402 and res.json()["code"] == "plan_limit"


def test_messages_stop_at_the_plan_limit(workspace):
    adapter = FakeAdapter("email")
    registry.register("email", lambda: adapter)
    workspace.organization.limits = {"messages_per_month": 1}
    workspace.organization.save()
    person = contact(workspace)
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=person.pk,
        channel="email",
        purpose="transactional",
        status="opted_in",
        source="form",
    )

    def send(key):
        return gateway.send(
            workspace_id=workspace.pk,
            actor=SYSTEM,
            contact_id=person.pk,
            channel="email",
            body="Hi",
            subject="S",
            idempotency_key=key,
        )

    assert not send("a").blocked
    blocked = send("b")
    assert blocked.blocked and blocked.reason == "limit_reached"


def test_only_the_first_publish_of_an_automation_needs_room(workspace):
    workspace.organization.limits = {"workflows": 1}
    workspace.organization.save()
    body = {
        "trigger": {"type": "contact_created", "config": {}},
        "start": "w",
        "steps": [{"id": "w", "type": "wait", "config": {"seconds": 5}}],
    }
    first = definitions.create_workflow(workspace.pk, name="One")
    second = definitions.create_workflow(workspace.pk, name="Two")
    definitions.save_draft(workspace.pk, first.pk, body)
    definitions.save_draft(workspace.pk, second.pk, body)
    definitions.publish(workspace.pk, first.pk)
    with pytest.raises(services.PlanLimitError):
        definitions.publish(workspace.pk, second.pk)
    definitions.save_draft(workspace.pk, first.pk, body)  # editing a live one is always allowed
    assert definitions.publish(workspace.pk, first.pk).published_version.number == 2


def test_the_plan_screen_data(workspace, owner):
    contact(workspace)
    client = Client()
    client.force_login(owner)
    data = client.get("/api/v1/crm/plan", HTTP_X_WORKSPACE_ID=str(workspace.pk)).json()
    by_key = {item["key"]: item for item in data["limits"]}
    assert data["plan_key"] == "free" and by_key["contacts"]["used"] == 1
    assert by_key["messages_per_month"]["limit"] == 300
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    assert viewer.get("/api/v1/crm/plan", HTTP_X_WORKSPACE_ID=str(workspace.pk)).status_code == 403
    other = create_workspace(name="Other Co", owner=owner)
    mine = client.get("/api/v1/crm/plan", HTTP_X_WORKSPACE_ID=str(other.pk)).json()
    assert {i["key"]: i["used"] for i in mine["limits"]}["contacts"] == 0


def test_operators_change_plans_with_a_command(workspace):
    out = StringIO()
    call_command("crm_set_plan", workspace=str(workspace.pk), plan="growth", stdout=out)
    workspace.organization.refresh_from_db()
    assert workspace.organization.plan_key == "growth" and "growth" in out.getvalue()
    with pytest.raises(CommandError):
        call_command("crm_set_plan", workspace=str(workspace.pk), plan="nope")
    with pytest.raises(CommandError):
        call_command("crm_set_plan", workspace="not-an-id", plan="free")


def test_the_onboarding_checklist_ticks_off_as_the_workspace_fills_up(workspace, owner):
    from crm.inbox import services as inbox
    from crm.pipelines import services as pipelines
    from crm.plans import onboarding
    from crm.templates import services as templates

    def done():
        return {s.key: s.done for s in onboarding.checklist(workspace.pk)}

    assert set(done().values()) == {False}
    contact(workspace)
    inbox.add_account(workspace.pk, channel="sms", provider="twilio", external_id="+14165550100")
    pipelines.create_pipeline(workspace.pk, name="Sales", stages=[("New", 10)])
    templates.create(workspace_id=workspace.pk, actor=SYSTEM, name="Hi", body="Hello")
    make_user("mate@example.com", workspace, Role.EDITOR)
    assert set(done().values()) == {True}
    client = Client()
    client.force_login(owner)
    res = client.get("/api/v1/crm/onboarding", HTTP_X_WORKSPACE_ID=str(workspace.pk))
    assert [s["key"] for s in res.json()] == ["contacts", "channel", "pipeline", "template", "team"]
    other = create_workspace(name="Other Co", owner=owner)
    fresh = client.get("/api/v1/crm/onboarding", HTTP_X_WORKSPACE_ID=str(other.pk)).json()
    assert not any(s["done"] for s in fresh if s["key"] != "team")
