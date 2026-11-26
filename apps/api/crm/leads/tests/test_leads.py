import json
import time
import uuid

import pytest
from django.test import Client

from audit.actors import Actor
from audit.outbox import dispatch_batch
from conftest import make_user
from core.errors import NotFoundError, ValidationFailedError
from crm.activity.models import Activity
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.contacts.models import Contact
from crm.fields import services as fields
from crm.leads import capi, mapping, services
from crm.leads.models import ConversionLog, LeadCapture
from crm.meta import services as meta
from crm.meta.graph import signature_of
from crm.meta.simulator import SimulatedGraph
from crm.pipelines import services as pipelines
from workspaces import services as workspace_services
from workspaces.models import RecordScope, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
APP_SECRET = "app-secret-value"
LEAD = "L100"


@pytest.fixture
def graph(monkeypatch):
    sim = SimulatedGraph()
    monkeypatch.setattr(meta, "transport_factory", sim.transport)
    sim.leads[LEAD] = lead_payload()
    return sim


@pytest.fixture
def account(workspace, graph):
    return meta.connect(
        workspace.pk, app_secret=APP_SECRET, page_token="page-token", page_id="PG1", pixel_id="PX1"
    )


def lead_payload(**over):
    base = {
        "id": LEAD,
        "form_id": "F1",
        "ad_id": "AD1",
        "adset_id": "AS1",
        "campaign_id": "CP1",
        "field_data": [
            {"name": "full_name", "values": ["Ada Lovelace"]},
            {"name": "email", "values": ["Ada@Example.com"]},
            {"name": "phone_number", "values": ["+14165550111"]},
            {"name": "city", "values": ["Toronto"]},
            {"name": "How soon do you need us?", "values": ["This month"]},
        ],
    }
    return base | over


def webhook(account, leadgen_id=LEAD):
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "PG1",
                "time": int(time.time()),
                "changes": [
                    {
                        "field": "leadgen",
                        "value": {"leadgen_id": leadgen_id, "form_id": "F1", "page_id": "PG1"},
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    return Client().post(
        f"/api/v1/crm-public/meta/{account.public_id}",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature_of(APP_SECRET, body),
    )


# ---- mapping -----------------------------------------------------------------------------------


def test_standard_questions_fill_the_contact_and_custom_ones_follow_the_mapping():
    data, answers = mapping.to_contact_data(
        lead_payload()["field_data"],
        {"How soon do you need us?": "custom.timeline", "city": "state"},
    )
    assert data["first_name"] == "Ada" and data["last_name"] == "Lovelace"
    assert data["email"] == "Ada@Example.com" and data["phone"] == "+14165550111"
    assert data["state"] == "Toronto" and "city" not in data  # an explicit mapping wins
    assert data["custom"] == {"timeline": "This month"}
    assert answers["How soon do you need us?"] == "This month"


def test_blank_answers_and_unknown_questions_are_skipped_safely():
    data, answers = mapping.to_contact_data(
        [
            {"name": "email", "values": []},
            {"name": "", "values": ["x"]},
            {"name": "zzz", "values": ["v"]},
        ],
        {},
    )
    assert data == {} and answers == {"zzz": "v"}


# ---- capture -----------------------------------------------------------------------------------


def test_a_lead_delivered_twice_makes_one_contact_and_one_activity(workspace, account, graph):
    assert webhook(account).status_code == 200
    assert webhook(account).status_code == 200
    dispatch_batch()
    assert Contact.objects.count() == 1 and LeadCapture.objects.count() == 1
    contact = Contact.objects.get()
    assert contact.email == "ada@example.com" and contact.source == "meta_lead_ad"
    assert contact.attribution["campaign_id"] == "CP1" and contact.attribution["ad_id"] == "AD1"
    assert Activity.objects.filter(kind="lead.captured").count() == 1


def test_an_existing_contact_is_updated_not_duplicated(workspace, account, graph):
    contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"email": "ada@example.com", "first_name": "A"},
    )
    webhook(account)
    assert Contact.objects.count() == 1
    assert LeadCapture.objects.get().contact.first_name == "Ada"


def test_form_rules_add_tags_and_record_the_consent_the_form_covers(workspace, account, graph):
    fields.create_definition(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        object_type="contact",
        key="timeline",
        label="Timeline",
        field_type="text",
    )
    services.save_mapping(
        workspace.pk,
        form_id="F1",
        name="Spring offer",
        field_map={"How soon do you need us?": "custom.timeline"},
        tags=["meta-lead", "spring"],
        consent_channels=["email"],
    )
    contacts_before = Contact.objects.count()
    webhook(account)
    contact = Contact.objects.get()
    assert contacts_before == 0 and set(contacts.tag_names_of([contact.pk])[contact.pk]) == {
        "meta-lead",
        "spring",
    }
    assert consent.has_explicit_opt_in(workspace.pk, contact.pk)


def test_no_consent_is_assumed_without_a_form_rule(workspace, account, graph):
    webhook(account)
    assert not consent.has_explicit_opt_in(workspace.pk, Contact.objects.get().pk)


def test_a_lead_without_email_or_phone_is_refused(workspace, account, graph):
    graph.leads["L2"] = lead_payload(id="L2", field_data=[{"name": "city", "values": ["X"]}])
    with pytest.raises(ValidationFailedError):
        services.capture(workspace.pk, leadgen_id="L2")
    assert LeadCapture.objects.count() == 0


def test_temporary_meta_trouble_is_retried_by_the_job(workspace, account, graph):
    from celery.exceptions import Retry

    from crm.leads.tasks import capture_lead

    kwargs = {"workspace_id": str(workspace.pk), "leadgen_id": LEAD}
    graph.fail_next(503, 2)
    with pytest.raises(Retry):  # the task asked to run again, with backoff
        capture_lead.apply(kwargs=kwargs).get()
    assert capture_lead.apply(kwargs=kwargs).get() == "stored"
    assert capture_lead.apply(kwargs=kwargs).get() == "duplicate"


def test_form_mapping_validation(workspace):
    for bad in (
        {"form_id": ""},
        {"form_id": "F", "consent_channels": ["carrier-pigeon"]},
        {"form_id": "F", "field_map": {"q": ""}},
        {"form_id": "F", "tags": ["a", "a"]},
    ):
        with pytest.raises(ValidationFailedError):
            services.save_mapping(workspace.pk, **bad)
    first = services.save_mapping(workspace.pk, form_id="F", name="One")
    again = services.save_mapping(workspace.pk, form_id="F", name="Two")
    assert first.pk == again.pk and again.name == "Two"


# ---- conversions -------------------------------------------------------------------------------


def opted_in_lead(workspace, account, graph):
    services.save_mapping(workspace.pk, form_id="F1", consent_channels=["email"])
    webhook(account)
    dispatch_batch()
    return Contact.objects.get()


def sent_events(graph):
    return [b for m, p, b in graph.requests if p.endswith("/PX1/events")]


def test_consenting_leads_are_reported_with_hashed_identifiers(workspace, account, graph):
    capi.create_rule(workspace.pk, name="Leads", trigger="lead_captured", event_name="Lead")
    opted_in_lead(workspace, account, graph)
    dispatch_batch()
    (body,) = sent_events(graph)
    event = body["data"][0]
    assert event["event_name"] == "Lead" and event["action_source"] == "system_generated"
    assert event["user_data"]["lead_id"] == LEAD
    assert "ada@example.com" not in json.dumps(event)  # only hashes leave
    assert len(event["user_data"]["em"][0]) == 64
    assert ConversionLog.objects.get().outcome == "sent"


def test_people_who_did_not_agree_are_never_reported(workspace, account, graph):
    capi.create_rule(workspace.pk, name="Leads", trigger="lead_captured", event_name="Lead")
    webhook(account)  # no form rule: no consent recorded
    dispatch_batch()
    assert sent_events(graph) == []
    assert ConversionLog.objects.get().outcome == "skipped_no_consent"


def test_won_deals_and_stage_milestones_carry_the_value(workspace, account, graph):
    contact = opted_in_lead(workspace, account, graph)
    pipeline = pipelines.create_pipeline(
        workspace.pk, name="Sales", stages=[("New", 10), ("Qualified", 40)]
    )
    qualified = pipeline.stages.get(name="Qualified")
    capi.create_rule(
        workspace.pk, name="Won", trigger="deal_won", event_name="Purchase", send_value=True
    )
    capi.create_rule(
        workspace.pk,
        name="Qualified",
        trigger="stage_reached",
        event_name="QualifiedLead",
        stage_id=qualified.pk,
    )
    deal = pipelines.create_opportunity(
        workspace.pk,
        SYSTEM,
        contact_id=contact.pk,
        pipeline_id=pipeline.pk,
        name="Roof",
        value_micros=2_500_000_000,
        currency="CAD",
    )
    pipelines.move_opportunity(workspace.pk, SYSTEM, deal, qualified.pk)
    pipelines.win(workspace.pk, SYSTEM, deal)
    dispatch_batch()
    names = {e["data"][0]["event_name"]: e["data"][0] for e in sent_events(graph)}
    assert set(names) == {"QualifiedLead", "Purchase"}
    assert names["Purchase"]["custom_data"] == {"value": 2500.0, "currency": "CAD"}
    assert "custom_data" not in names["QualifiedLead"]


def test_a_failed_report_is_logged_and_a_temporary_one_is_retried(workspace, account, graph):
    from core.errors import RetryableProviderError

    contact = opted_in_lead(workspace, account, graph)
    capi.create_rule(workspace.pk, name="Leads", trigger="lead_captured", event_name="Lead")
    graph.fail_next(400, 100)
    assert (
        capi.report(
            workspace.pk, trigger="lead_captured", source_event_id="e1", contact_id=contact.pk
        )
        == 0
    )
    assert ConversionLog.objects.get().outcome == "failed"
    graph.fail_next(503, 2)
    with pytest.raises(RetryableProviderError):
        capi.report(
            workspace.pk, trigger="lead_captured", source_event_id="e2", contact_id=contact.pk
        )
    assert (
        capi.report(
            workspace.pk, trigger="lead_captured", source_event_id="e2", contact_id=contact.pk
        )
        == 1
    )
    assert (
        capi.report(  # the same source event is never reported twice
            workspace.pk, trigger="lead_captured", source_event_id="e2", contact_id=contact.pk
        )
        == 0
    )


def test_rule_validation(workspace):
    for bad in (
        {"name": "x", "trigger": "nope", "event_name": "Lead"},
        {"name": "x", "trigger": "deal_won", "event_name": "bad name!"},
        {"name": "x", "trigger": "stage_reached", "event_name": "Lead"},
        {"name": "x", "trigger": "deal_won", "event_name": "Lead", "stage_id": uuid.uuid4()},
        {"name": " ", "trigger": "deal_won", "event_name": "Lead"},
    ):
        with pytest.raises(ValidationFailedError):
            capi.create_rule(workspace.pk, **bad)


# ---- API ---------------------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def logged(user):
    c = Client()
    c.force_login(user)
    return c


def test_api_roundtrip(workspace, owner, account, graph):
    admin = logged(owner)
    saved = api(
        admin,
        "put",
        "/leads/forms",
        workspace,
        {"form_id": "F1", "name": "Spring", "field_map": {"q": "custom.x"}, "tags": ["a"]},
    )
    assert saved.status_code == 200 and saved.json()["field_map"] == {"q": "custom.x"}
    webhook(account)
    leads = api(admin, "get", "/leads", workspace).json()
    assert len(leads) == 1 and leads[0]["campaign_id"] == "CP1"
    made = api(
        admin,
        "post",
        "/leads/conversion-rules",
        workspace,
        {"name": "Leads", "trigger": "lead_captured", "event_name": "Lead"},
    )
    assert made.status_code == 201
    rid = made.json()["id"]
    off = api(admin, "patch", f"/leads/conversion-rules/{rid}", workspace, {"is_active": False})
    assert off.json()["is_active"] is False
    assert api(admin, "get", "/leads/conversions", workspace).json() == []
    assert api(admin, "delete", f"/leads/conversion-rules/{rid}", workspace).status_code == 204
    form_id = api(admin, "get", "/leads/forms", workspace).json()[0]["id"]
    assert api(admin, "delete", f"/leads/forms/{form_id}", workspace).status_code == 204


def test_permissions_scope_and_tenants(workspace, owner, account, graph):
    webhook(account)
    viewer = logged(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, "get", "/leads", workspace).status_code == 200
    assert api(viewer, "get", "/leads/forms", workspace).status_code == 403
    assert api(viewer, "get", "/leads/conversion-rules", workspace).status_code == 403
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    workspace_services.set_record_scope(
        workspace=workspace, user=rep, scope=RecordScope.ASSIGNED, changed_by=owner
    )
    assert api(logged(rep), "get", "/leads", workspace).json() == []  # not their contact
    other = create_workspace(name="Other Co", owner=owner)
    admin = logged(owner)
    assert api(admin, "get", "/leads", other).json() == []
    assert api(admin, "get", "/leads/forms", other).json() == []
    assert api(admin, "delete", f"/leads/forms/{uuid.uuid4()}", workspace).status_code == 404
    with pytest.raises(NotFoundError):
        services.get_mapping(other.pk, uuid.uuid4())
