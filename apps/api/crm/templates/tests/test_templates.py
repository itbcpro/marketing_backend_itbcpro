import json
import uuid

import pytest
from django.test import Client

from audit.actors import Actor
from conftest import make_user
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.contacts import services as contacts
from crm.fields import services as fields
from crm.templates import services
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def person(workspace):
    fields.create_definition(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        object_type="contact",
        key="plan",
        label="Plan",
        field_type="select",
        options=[{"value": "gold"}],
    )
    fields.set_workspace_value(
        workspace_id=workspace.pk, key="booking_link", label="Booking", value="https://x.test/book"
    )
    company = contacts.create_company(
        workspace_id=workspace.pk, actor=SYSTEM, data={"name": "Acme Inc"}
    )
    return contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "city": "Toronto",
            "company_id": company.pk,
            "custom": {"plan": "gold"},
        },
    )


def make(workspace, name="Welcome", body="Hi {{contact.first_name}}", **extra):
    return services.create(workspace_id=workspace.pk, actor=SYSTEM, name=name, body=body, **extra)


def test_templates_render_every_namespace(workspace, person):
    row = make(
        workspace,
        subject="Hello {{contact.first_name}} from {{company.name}}",
        body="Plan: {{contact.plan}} in {{contact.city}}. Book: {{workspace_values.booking_link}}. Stop: {{system.unsubscribe_url}}",
    )
    out = services.render(workspace_id=workspace.pk, template_id=row.pk, contact_id=person.pk)
    assert out.subject == "Hello Ada from Acme Inc"
    assert out.body.startswith("Plan: gold in Toronto. Book: https://x.test/book. Stop: ")
    assert "/unsubscribe/" in out.body


def test_missing_values_use_the_fallback_not_an_error(workspace):
    bare = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "x@example.com"}
    )
    row = make(workspace, body="Hi {{contact.first_name}}, {{company.name}}!")
    out = services.render(workspace_id=workspace.pk, template_id=row.pk, contact_id=bare.pk)
    assert out.body == "Hi , !"


def test_custom_values_of_other_types_read_naturally(workspace):
    fields.create_definition(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        object_type="contact",
        key="vip",
        label="V",
        field_type="checkbox",
    )
    fields.create_definition(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        object_type="contact",
        key="spend",
        label="S",
        field_type="money",
    )
    c = contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={
            "email": "m@example.com",
            "custom": {"vip": True, "spend": {"amount": "12.5", "currency": "cad"}},
        },
    )
    row = make(workspace, body="{{contact.vip}} / {{contact.spend}}")
    assert (
        services.render(workspace_id=workspace.pk, template_id=row.pk, contact_id=c.pk).body
        == "yes / 12.500000 CAD"
    )


@pytest.mark.parametrize(
    "body",
    [
        "{{contact.nope}}",
        "{{company.secret}}",
        "{{workspace_values.missing}}",
        "{{system.password}}",
        "{{evil.thing}}",
    ],
)
def test_unknown_merge_fields_are_rejected_when_saving(workspace, body):
    with pytest.raises(ValidationFailedError) as caught:
        make(workspace, body=body)
    assert len(caught.value.params["errors"]) == 1


def test_validation_of_the_other_fields(workspace):
    for bad in (
        {"name": " "},
        {"body": " "},
        {"kind": "poster"},
        {"channel": "pigeon"},
        {"subject": "x" * 301},
    ):
        with pytest.raises(ValidationFailedError):
            make(workspace, **{"name": "N", "body": "b", **bad})


def test_names_are_unique_among_active_templates(workspace):
    first = make(workspace)
    with pytest.raises(ConflictError):
        make(workspace)
    services.archive(workspace_id=workspace.pk, actor=SYSTEM, template_id=first.pk)
    assert make(workspace).pk != first.pk
    assert [t.name for t in services.list_templates(workspace.pk)] == ["Welcome"]


def test_update_revalidates_and_filters_by_channel_and_kind(workspace, person):
    row = make(workspace, channel="email", subject="S")
    snippet = make(workspace, name="Thanks", body="Thanks!", kind="snippet")
    updated = services.update(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        template_id=row.pk,
        data={"body": "Bye {{contact.last_name}}"},
    )
    assert updated.body == "Bye {{contact.last_name}}" and updated.subject == "S"
    with pytest.raises(ValidationFailedError):
        services.update(
            workspace_id=workspace.pk,
            actor=SYSTEM,
            template_id=row.pk,
            data={"body": "{{contact.nope}}"},
        )
    assert [t.name for t in services.list_templates(workspace.pk, kind="snippet")] == ["Thanks"]
    assert {t.name for t in services.list_templates(workspace.pk, channel="sms")} == {
        "Thanks"
    }  # any-channel only
    assert {t.name for t in services.list_templates(workspace.pk, channel="email")} == {
        "Welcome",
        "Thanks",
    }
    assert snippet.channel == ""


def test_templates_do_not_cross_workspaces(workspace, owner):
    row = make(workspace)
    other = create_workspace(name="Other Co", owner=owner)
    with pytest.raises(NotFoundError):
        services.get(other.pk, row.pk)
    assert make_in(other).pk != row.pk  # the same name is free in another workspace


def make_in(workspace):
    return services.create(workspace_id=workspace.pk, actor=SYSTEM, name="Welcome", body="Hi")


# ---- API --------------------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def test_api_roundtrip_preview_and_fields(workspace, owner, person):
    admin = Client()
    admin.force_login(owner)
    created = api(
        admin,
        "post",
        "/templates",
        workspace,
        {"name": "Hi", "body": "Hi {{contact.first_name}}", "subject": "S"},
    )
    assert created.status_code == 201
    tid = created.json()["id"]
    assert api(admin, "get", f"/templates/{tid}", workspace).json()["name"] == "Hi"
    patched = api(admin, "patch", f"/templates/{tid}", workspace, {"subject": "New"})
    assert patched.json()["subject"] == "New"
    preview = api(
        admin, "post", f"/templates/{tid}/preview", workspace, {"contact_id": str(person.pk)}
    )
    assert preview.json() == {"subject": "New", "body": "Hi Ada"}
    draft = api(
        admin,
        "post",
        f"/templates/{tid}/preview",
        workspace,
        {"contact_id": str(person.pk), "body": "Yo {{contact.city}}"},
    )
    assert draft.json()["body"] == "Yo Toronto"
    fields_out = api(admin, "get", "/template-fields", workspace).json()["namespaces"]
    assert "plan" in fields_out["contact"] and fields_out["workspace_values"] == ["booking_link"]
    assert (
        api(
            admin, "post", "/templates", workspace, {"name": "Bad", "body": "{{nope.x}}"}
        ).status_code
        == 422
    )
    assert api(admin, "delete", f"/templates/{tid}", workspace).status_code == 204
    assert api(admin, "get", "/templates", workspace).json() == []


def test_api_permissions_scope_and_tenants(workspace, owner, person):
    viewer = Client()
    viewer.force_login(make_user("v@example.com", workspace, Role.VIEWER))
    admin = Client()
    admin.force_login(owner)
    body = {"name": "X", "body": "x"}
    assert api(viewer, "post", "/templates", workspace, body).status_code == 403
    tid = api(admin, "post", "/templates", workspace, body).json()["id"]
    assert api(viewer, "get", "/templates", workspace).status_code == 200
    other = create_workspace(name="Other Co", owner=owner)
    for method, path, payload in (
        ("get", f"/templates/{tid}", None),
        ("patch", f"/templates/{tid}", {"name": "Z"}),
        ("delete", f"/templates/{tid}", None),
        ("post", f"/templates/{tid}/preview", {"contact_id": str(person.pk)}),
    ):
        assert api(admin, method, path, other, payload).status_code == 404, path
    assert (
        api(
            admin, "post", f"/templates/{tid}/preview", workspace, {"contact_id": str(uuid.uuid4())}
        ).status_code
        == 404
    )
