"""Importing deals (opportunities) and the contact-import additions: companies and comma tags."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from audit.actors import Actor
from core.errors import ValidationFailedError
from crm.activity.models import Note
from crm.contacts import services as contacts
from crm.lists import imports
from crm.lists.models import ImportKind
from crm.pipelines import services as pipelines
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
DEALS = (
    "Opportunity name,Contact Name,phone,email,pipeline,stage,Lead Value,source,assigned,"
    "Created on,Notes,tags,status,lost reason name\n"
    "Ada Lovelace,Ada Lovelace,+14165550199,ada@example.com,Sales - Bark,Contacted,1500,Bark,"
    'Owner,2026-11-25T15:30:54.346Z,"Called twice\nno answer","bark, follow-up",open,\n'
    "Bob Deal,Bob Stone,,bob@example.com,Sales - Bark,Dead,0,Bark,Nobody Here,,,,lost,No budget\n"
    "Cy,Cy Young,+14165550123,,Clients,,,,,,,,,\n"
)
CONTACTS = (
    "Contact Id,First Name,Last Name,Phone,Email,Business Name,Created,Tags\n"
    "x1,Ada,Lovelace,+14165550199,ada@example.com,Analytical Engines,2026-11-25,"
    '"website lead, customer"\n'
    "x2,Bob,Stone,,bob@example.com,analytical engines,2026-11-25,bark\n"
)


def run(workspace, text, kind, options=None):
    job = imports.create_import(
        workspace_id=workspace.pk, actor=SYSTEM, file_name="f.csv", raw=text.encode(), kind=kind
    )
    imports.start(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        job_id=job.pk,
        mapping=job.mapping,
        options=options or {},
    )
    return imports.get_job(workspace.pk, job.pk)


def test_headers_of_a_gohighlevel_export_are_mapped_to_deal_targets(workspace):
    job = imports.create_import(
        workspace_id=workspace.pk, actor=SYSTEM, file_name="o.csv", raw=DEALS.encode(), kind="deals"
    )
    assert job.kind == ImportKind.DEALS
    assert job.mapping["Opportunity name"] == "name"
    assert job.mapping["Contact Name"] == "contact_name"
    assert job.mapping["phone"] == "contact_phone"
    assert job.mapping["email"] == "contact_email"
    assert job.mapping["Lead Value"] == "value"
    assert job.mapping["assigned"] == "owner"
    assert job.mapping["Created on"] == "created_at"
    assert job.mapping["lost reason name"] == "lost_reason"


def test_deals_are_created_with_pipelines_contacts_notes_owner_and_age(workspace, owner):
    job = run(workspace, DEALS, "deals", {"tags": ["imported"]})
    assert (job.created_count, job.failed_count) == (3, 0), job.errors
    assert {p.name for p in pipelines.list_pipelines(workspace.pk)} == {"Sales - Bark", "Clients"}
    ada = contacts.find_contact(workspace.pk, "email", "ada@example.com")
    deal = pipelines.list_opportunities(workspace.pk).get(contact_id=ada.pk)
    assert deal.stage.name == "Contacted" and deal.value_micros == 1_500_000_000
    assert deal.assigned_to_id == owner.pk and deal.source == "Bark"
    assert deal.tags == ["imported", "bark", "follow-up"]
    assert deal.created_at.isoformat().startswith("2026-11-25T15:30:54")
    notes = Note.objects.filter(contact_id=ada.pk, opportunity_id=deal.pk)
    assert [n.body for n in notes] == ["Called twice\nno answer"]
    lost = pipelines.list_opportunities(workspace.pk).get(name="Bob Deal")
    assert lost.status == "lost" and lost.lost_reason.name == "No budget"
    assert lost.assigned_to_id is None  # an unknown owner leaves the deal unassigned
    cy = contacts.find_contact(workspace.pk, "phone", "+14165550123")
    assert (cy.first_name, cy.last_name) == ("Cy", "Young")


def test_a_second_run_creates_nothing_new(workspace):
    run(workspace, DEALS, "deals")
    again = run(workspace, DEALS, "deals")
    assert (again.created_count, again.updated_count) == (0, 3)
    assert pipelines.list_opportunities(workspace.pk).count() == 3


def test_unknown_pipeline_or_contact_fails_when_creating_is_off(workspace):
    job = run(workspace, DEALS, "deals", {"create_missing": False})
    assert job.failed_count == 3 and job.created_count == 0
    assert job.errors[0]["errors"] == {"pipeline": "unknown"}


def test_dry_run_reports_bad_rows_and_mapping_needs_pipeline_and_contact(workspace):
    text = (
        "name,email,pipeline,Lead Value,status\n"
        "ok,a@example.com,P,10,open\n"
        "bad value,b@example.com,P,ten,open\n"
        "bad status,c@example.com,P,1,done\n"
        "no contact,,P,1,open\n"
    )
    job = imports.create_import(
        workspace_id=workspace.pk, actor=SYSTEM, file_name="d.csv", raw=text.encode(), kind="deals"
    )
    result = imports.dry_run(workspace_id=workspace.pk, job=job, mapping=job.mapping)
    assert result["valid"] == 1 and result["invalid"] == 3
    assert [p["row"] for p in result["problems"]] == [3, 4, 5]
    with pytest.raises(ValidationFailedError) as caught:
        imports.dry_run(
            workspace_id=workspace.pk, job=job, mapping={**job.mapping, "pipeline": "ignore"}
        )
    assert caught.value.params["errors"]["mapping"] == "pipeline_required"
    with pytest.raises(ValidationFailedError):
        imports.dry_run(
            workspace_id=workspace.pk, job=job, mapping={**job.mapping, "email": "ignore"}
        )


def test_contact_import_creates_companies_and_splits_tags_on_commas(workspace):
    job = run(workspace, CONTACTS, "contacts")
    assert job.created_count == 2, job.errors
    ada = contacts.find_contact(workspace.pk, "email", "ada@example.com")
    bob = contacts.find_contact(workspace.pk, "email", "bob@example.com")
    assert ada.company_id == bob.company_id and ada.company.name == "Analytical Engines"
    assert contacts.tag_names_of([ada.pk])[ada.pk] == ["customer", "website lead"]


def test_deal_import_api_and_tenant_isolation(workspace, owner):
    client = Client()
    client.force_login(owner)
    header = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    file = SimpleUploadedFile("o.csv", DEALS.encode(), content_type="text/csv")
    response = client.post("/api/v1/crm/imports?kind=deals", {"file": file}, **header)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["kind"] == "deals" and "pipeline" in body["targets"]
    bad = SimpleUploadedFile("o.csv", DEALS.encode(), content_type="text/csv")
    assert client.post("/api/v1/crm/imports?kind=nope", {"file": bad}, **header).status_code == 422
    other = create_workspace(name="Other Co", owner=owner)
    stranger = {"HTTP_X_WORKSPACE_ID": str(other.pk)}
    assert client.get(f"/api/v1/crm/imports/{body['id']}", **stranger).status_code == 404
