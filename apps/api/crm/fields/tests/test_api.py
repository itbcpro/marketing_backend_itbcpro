import json

import pytest
from django.test import Client

from conftest import make_user
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
BASE = "/api/v1/crm"


def call(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"{BASE}{path}", **kwargs)


@pytest.fixture
def api(owner):
    client = Client()
    client.force_login(owner)
    return client


def test_field_lifecycle_through_the_api(api, workspace):
    created = call(
        api,
        "post",
        "/fields",
        workspace,
        {
            "object_type": "contact",
            "key": "tier",
            "label": "Tier",
            "field_type": "select",
            "options": [{"value": "gold"}],
        },
    )
    assert created.status_code == 201
    field_id = created.json()["id"]
    assert call(api, "get", "/fields?object_type=contact", workspace).json()[0]["key"] == "tier"
    patched = call(api, "patch", f"/fields/{field_id}", workspace, {"label": "Level"})
    assert patched.json()["label"] == "Level"
    assert call(api, "post", f"/fields/{field_id}/archive", workspace).status_code == 204
    assert call(api, "get", "/fields?object_type=contact", workspace).json() == []
    assert (
        len(call(api, "get", "/fields?object_type=contact&include_archived=true", workspace).json())
        == 1
    )


def test_validation_errors_name_the_field_and_reason(api, workspace):
    response = call(
        api,
        "post",
        "/fields",
        workspace,
        {"object_type": "contact", "key": "email", "label": "X", "field_type": "text"},
    )
    assert response.status_code == 422
    assert response.json()["errors"] == [{"loc": ["key"], "type": "reserved_key"}]


def test_duplicate_key_is_a_conflict(api, workspace):
    body = {"object_type": "contact", "key": "dup", "label": "Dup", "field_type": "text"}
    assert call(api, "post", "/fields", workspace, body).status_code == 201
    assert call(api, "post", "/fields", workspace, body).status_code == 409


def test_workspace_values_crud(api, workspace):
    put = call(
        api,
        "put",
        "/workspace-values/booking_link",
        workspace,
        {"label": "Booking", "value": "https://x.test"},
    )
    assert put.status_code == 200
    assert call(api, "get", "/workspace-values", workspace).json()[0]["value"] == "https://x.test"
    assert call(api, "delete", "/workspace-values/booking_link", workspace).status_code == 204
    assert call(api, "delete", "/workspace-values/booking_link", workspace).status_code == 404


def test_permissions_viewers_read_but_cannot_change(workspace):
    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    client = Client()
    client.force_login(viewer)
    assert call(client, "get", "/fields", workspace).status_code == 200
    body = {"object_type": "contact", "key": "nope", "label": "N", "field_type": "text"}
    assert call(client, "post", "/fields", workspace, body).status_code == 403


def test_cross_tenant_ids_are_not_found(api, workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    body = {"object_type": "contact", "key": "secret", "label": "S", "field_type": "text"}
    field_id = call(api, "post", "/fields", workspace, body).json()["id"]
    assert call(api, "patch", f"/fields/{field_id}", other, {"label": "hijack"}).status_code == 404
    assert call(api, "post", f"/fields/{field_id}/archive", other).status_code == 404
    assert call(api, "get", "/fields?object_type=contact", other).json() == []


def test_outsiders_cannot_use_the_workspace(workspace):
    outsider = make_user("outsider@example.com")
    client = Client()
    client.force_login(outsider)
    assert call(client, "get", "/fields", workspace).status_code == 422


def _field(api, workspace, key, folder="", object_type="contact"):
    body = {
        "object_type": object_type,
        "key": key,
        "label": key.title(),
        "field_type": "text",
        "folder": folder,
    }
    response = call(api, "post", "/fields", workspace, body)
    assert response.status_code == 201, response.content
    return response.json()


def test_fields_carry_their_creation_time(api, workspace):
    assert _field(api, workspace, "plan")["created_at"]


def test_renaming_a_folder_moves_only_its_fields(api, workspace):
    _field(api, workspace, "plan", folder="Sales")
    _field(api, workspace, "tier", folder="Sales")
    _field(api, workspace, "forecast", folder="Sales", object_type="opportunity")
    _field(api, workspace, "memo", folder="Misc")
    body = {"object_type": "contact", "old": "Sales", "new": "Revenue"}
    assert call(api, "post", "/fields/folders/rename", workspace, body).json() == {"changed": 2}
    listed = {f["key"]: f["folder"] for f in call(api, "get", "/fields", workspace).json()}
    assert listed == {"plan": "Revenue", "tier": "Revenue", "memo": "Misc"}
    opp = call(api, "get", "/fields?object_type=opportunity", workspace).json()
    assert [f["folder"] for f in opp] == ["Sales"]
    clear = {"object_type": "contact", "old": "Revenue", "new": ""}
    assert call(api, "post", "/fields/folders/rename", workspace, clear).json() == {"changed": 2}
    again = call(api, "get", "/fields", workspace).json()
    assert {f["key"]: f["folder"] for f in again} == {"plan": "", "tier": "", "memo": "Misc"}


def test_folder_rename_validates_permissions_and_tenants(api, workspace, owner):
    _field(api, workspace, "plan", folder="Sales")
    bad = {"object_type": "nothing", "old": "Sales", "new": "X"}
    assert call(api, "post", "/fields/folders/rename", workspace, bad).status_code == 422
    long_name = {"object_type": "contact", "old": "Sales", "new": "x" * 101}
    assert call(api, "post", "/fields/folders/rename", workspace, long_name).status_code == 422
    other = create_workspace(name="Other Co", owner=owner)
    body = {"object_type": "contact", "old": "Sales", "new": "Hijacked"}
    assert call(api, "post", "/fields/folders/rename", other, body).json() == {"changed": 0}
    assert call(api, "get", "/fields", workspace).json()[0]["folder"] == "Sales"
    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    client = Client()
    client.force_login(viewer)
    assert call(client, "post", "/fields/folders/rename", workspace, body).status_code == 403
