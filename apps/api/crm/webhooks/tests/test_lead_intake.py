"""Lead intake webhook: Zapier, n8n or any tool posts a lead; it becomes a contact and a deal."""

import json
from urllib.parse import urlencode

import pytest
from django.test import Client

from audit.actors import Actor
from conftest import make_user
from core.errors import ValidationFailedError
from crm.activity.models import Note
from crm.contacts import services as contacts
from crm.contacts.models import Contact
from crm.pipelines import services as pipelines
from crm.webhooks import services
from crm.webhooks.models import DeliveryStatus, WebhookDelivery, WebhookKind
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
JSON = "application/json"
LEAD = {
    "first_name": "Jay",
    "email": "remedy_03@hotmail.com",
    "phone": "(604) 359-2238",
    "company": "Remedy Co",
    "postal_code": "V5Z3G1",
    "notes": "What type of website do you need?",
    "extra": {"Budget": "Less than $500"},
}


@pytest.fixture
def stage(workspace):
    return pipelines.create_pipeline(
        workspace.pk, name="ITBC Sales", stages=[("New Lead", 10), ("Contacted", 20)]
    ).stages.first()


@pytest.fixture
def source(workspace, owner, stage):
    config = {
        "stage_id": str(stage.pk),
        "owner_id": str(owner.pk),
        "source": "Bark",
        "tags": ["bark"],
    }
    created = services.create_endpoint(
        workspace.pk, kind=WebhookKind.LEAD_INTAKE, name="Bark", config=config
    )
    return created.endpoint


def post(source, data, form=False):
    url = f"/api/v1/crm-public/hooks/{source.public_id}"
    if form:
        return Client().post(url, urlencode(data), content_type="application/x-www-form-urlencoded")
    return Client().post(url, json.dumps(data), content_type=JSON)


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": JSON}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def test_a_lead_becomes_a_contact_a_deal_a_note_and_tags(workspace, owner, source):
    assert post(source, LEAD).status_code == 202
    contact = contacts.find_contact(workspace.pk, "email", "remedy_03@hotmail.com")
    assert contact.first_name == "Jay" and contact.source == "Bark"
    assert contact.assigned_to_id == owner.pk and contact.company.name == "Remedy Co"
    assert contacts.tag_names_of([contact.pk])[contact.pk] == ["bark"]
    deal = pipelines.list_opportunities(workspace.pk).get(contact_id=contact.pk)
    assert deal.stage.name == "New Lead" and deal.assigned_to_id == owner.pk
    assert deal.source == "Bark" and deal.name == "Jay"
    note = Note.objects.get(contact_id=contact.pk, opportunity_id=deal.pk)
    assert "What type of website" in note.body and "Budget: Less than $500" in note.body


def test_zapier_style_form_fields_work_and_a_repeat_adds_no_duplicates(workspace, source):
    form = {"Full Name": "Jay Kim", "Email": "jay@example.com", "Phone Number": "6043592238"}
    assert post(source, form, form=True).status_code == 202
    again = {**form, "Message": "Called again"}  # a changed body is a new delivery
    assert post(source, again, form=True).status_code == 202
    assert Contact.objects.for_workspace(workspace.pk).count() == 1
    deal = pipelines.list_opportunities(workspace.pk).get()
    assert deal.name == "Jay Kim"
    assert Note.objects.get(opportunity_id=deal.pk).body == "Called again"


def test_an_existing_contact_is_not_overwritten(workspace, source):
    mine = contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"email": "jay@example.com", "first_name": "Jason", "source": "Referral"},
    )
    post(source, {"email": "jay@example.com", "first_name": "Jay"})
    mine.refresh_from_db()
    assert (mine.first_name, mine.source) == ("Jason", "Referral")
    assert pipelines.list_opportunities(workspace.pk).get().contact_id == mine.pk


def test_a_bad_phone_is_noted_and_a_lead_without_contact_details_is_refused(workspace, source):
    assert post(source, {"email": "a@example.com", "phone": "+10714434509"}).status_code == 202
    contact = contacts.find_contact(workspace.pk, "email", "a@example.com")
    assert contact.phone == ""
    assert "+10714434509" in Note.objects.get(contact_id=contact.pk).body
    assert post(source, {"first_name": "Nobody"}).status_code == 422
    rows = WebhookDelivery.objects.for_workspace(workspace.pk).order_by("-created_at")
    assert rows[0].status == DeliveryStatus.FAILED
    assert "email_or_phone_required" in rows[0].error


def test_the_request_can_choose_pipeline_stage_and_value(workspace, source):
    pipelines.create_pipeline(workspace.pk, name="Clients", stages=[("Onboarding", 50)])
    body = {
        "email": "b@example.com",
        "pipeline": "clients",
        "stage": "Onboarding",
        "value": "2,500",
    }
    assert post(source, body).status_code == 202
    deal = pipelines.list_opportunities(workspace.pk).get()
    assert deal.pipeline.name == "Clients" and deal.value_micros == 2_500_000_000
    assert post(source, {"email": "c@example.com", "pipeline": "Nope"}).status_code == 422
    assert contacts.find_contact(workspace.pk, "email", "c@example.com") is None


def test_without_a_stage_only_the_contact_is_created(workspace):
    created = services.create_endpoint(workspace.pk, kind=WebhookKind.LEAD_INTAKE, name="Plain")
    assert post(created.endpoint, {"email": "d@example.com"}).status_code == 202
    assert contacts.find_contact(workspace.pk, "email", "d@example.com") is not None
    assert pipelines.list_opportunities(workspace.pk).count() == 0


def test_rotating_gives_a_new_url_and_the_old_one_stops_working(workspace, source):
    old = source.public_id
    rotated = services.rotate_secret(workspace.pk, source.pk).endpoint
    assert rotated.public_id != old
    assert (
        Client().post(f"/api/v1/crm-public/hooks/{old}", "{}", content_type=JSON).status_code == 404
    )
    assert post(rotated, LEAD).status_code == 202


def test_config_is_validated_and_tenants_stay_apart(workspace, owner, stage, source):
    other = create_workspace(name="Other Co", owner=owner)
    with pytest.raises(ValidationFailedError):  # a stage of another workspace
        services.create_endpoint(
            other.pk, kind=WebhookKind.LEAD_INTAKE, name="x", config={"stage_id": str(stage.pk)}
        )
    post(source, LEAD)
    assert contacts.find_contact(other.pk, "email", "remedy_03@hotmail.com") is None
    assert pipelines.list_opportunities(other.pk).count() == 0


def test_management_api_with_config_permissions_and_isolation(workspace, owner, stage):
    admin = Client()
    admin.force_login(owner)
    body = {"kind": "lead_intake", "name": "Bark", "config": {"stage_id": str(stage.pk)}}
    made = api(admin, "post", "/webhooks", workspace, body)
    assert made.status_code == 201, made.content
    row = made.json()
    assert row["config"]["stage_id"] == str(stage.pk)
    edit = {"name": "Bark leads", "config": {"stage_id": str(stage.pk), "tags": ["bark"]}}
    changed = api(admin, "put", f"/webhooks/{row['id']}", workspace, edit)
    assert changed.json()["name"] == "Bark leads" and changed.json()["config"]["tags"] == ["bark"]
    other = create_workspace(name="Other Co", owner=owner)
    assert api(admin, "put", f"/webhooks/{row['id']}", other, edit).status_code == 404
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, "put", f"/webhooks/{row['id']}", workspace, edit).status_code == 403
