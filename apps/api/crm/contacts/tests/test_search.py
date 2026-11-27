"""POST /contacts/search: any filter, text, sort, offset paging, record scope, tenant isolation."""

import pytest
from django.test import Client

from conftest import make_user
from crm.contacts.tests.test_api import call, create, login
from workspaces import services as workspace_services
from workspaces.models import RecordScope, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def api(owner):
    return login(owner)


def search(api, workspace, **body):
    return call(api, "post", "/contacts/search", workspace, body)


def names(response):
    assert response.status_code == 200, response.content
    return [c["first_name"] for c in response.json()["items"]]


def eq(field, value, operator="eq"):
    return {"op": "and", "conditions": [{"field": field, "operator": operator, "value": value}]}


def test_sorts_and_pages_by_offset(api, workspace):
    for name in ("Bea", "Cy", "Ada"):
        create(api, workspace, first_name=name, email=f"{name.lower()}@example.com")
    assert names(search(api, workspace, sort="name")) == ["Ada", "Bea", "Cy"]
    assert names(search(api, workspace, sort="oldest")) == ["Bea", "Cy", "Ada"]
    assert names(search(api, workspace)) == ["Ada", "Cy", "Bea"]  # newest first by default
    first = search(api, workspace, sort="name", limit=2).json()
    assert [c["first_name"] for c in first["items"]] == ["Ada", "Bea"] and first["next_cursor"]
    rest = search(api, workspace, sort="name", limit=2, cursor=first["next_cursor"]).json()
    assert [c["first_name"] for c in rest["items"]] == ["Cy"] and rest["next_cursor"] is None


def test_filter_language_and_text_combine(api, workspace):
    create(api, workspace, first_name="Ada", email="ada@acme.test", source="web", tags=["hot"])
    create(api, workspace, first_name="Bob", email="bob@acme.test", source="ads")
    create(api, workspace, first_name="Cy", email="cy@other.test", source="web")
    assert names(search(api, workspace, filter=eq("source", "web"), sort="name")) == ["Ada", "Cy"]
    assert names(search(api, workspace, filter=eq("source", "web"), q="acme")) == ["Ada"]
    assert names(search(api, workspace, filter=eq("tags", ["hot"], "has_any"))) == ["Ada"]
    either = {
        "op": "or",
        "conditions": [
            {"field": "source", "operator": "eq", "value": "ads"},
            {"field": "tags", "operator": "has_any", "value": ["hot"]},
        ],
    }
    assert names(search(api, workspace, filter=either, sort="name")) == ["Ada", "Bob"]
    never = eq("last_activity_at", None, "empty")
    assert len(names(search(api, workspace, filter=never))) == 3


def test_bad_input_is_refused(api, workspace):
    assert search(api, workspace, sort="size").status_code == 422
    assert search(api, workspace, cursor="abc").status_code == 422
    assert search(api, workspace, filter=eq("password", "x")).status_code == 422


def test_record_scope_and_other_workspaces(workspace, api, owner):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    workspace_services.set_record_scope(
        workspace=workspace, user=rep, scope=RecordScope.ASSIGNED, changed_by=owner
    )
    create(api, workspace, first_name="Mine", email="mine@example.com", assigned_to=str(rep.pk))
    create(api, workspace, first_name="Theirs", email="theirs@example.com")
    assert names(search(login(rep), workspace)) == ["Mine"]
    assert sorted(names(search(api, workspace))) == ["Mine", "Theirs"]
    other = create_workspace(name="Other Co", owner=owner)
    assert names(search(api, other)) == []
    viewer = login(make_user("v@example.com", workspace, Role.VIEWER))
    assert search(viewer, workspace).status_code == 200  # viewing is allowed
    assert search(Client(), workspace).status_code in {401, 403}
