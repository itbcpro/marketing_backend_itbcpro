import json
import uuid

import pytest
from django.test import Client

from conftest import make_user
from workspaces import services as workspace_services
from workspaces.models import RecordScope, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
BASE = "/api/v1/crm"


def call(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"{BASE}{path}", **kwargs)


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def api(owner):
    return login(owner)


def create(api, workspace, **data):
    response = call(api, "post", "/contacts", workspace, data)
    assert response.status_code == 201, response.content
    return response.json()


def test_create_get_update_delete_cycle(api, workspace):
    made = create(
        api, workspace, first_name="Ada", email="Ada@Example.com", tags=["VIP"], city="Toronto"
    )
    assert made["email"] == "ada@example.com" and made["tags"] == ["VIP"]
    got = call(api, "get", f"/contacts/{made['id']}", workspace).json()
    assert got["full_name"] == "Ada"
    patched = call(api, "patch", f"/contacts/{made['id']}", workspace, {"last_name": "Lovelace"})
    assert patched.json()["full_name"] == "Ada Lovelace"
    assert call(api, "delete", f"/contacts/{made['id']}", workspace).status_code == 204
    assert call(api, "get", f"/contacts/{made['id']}", workspace).status_code == 404
    gone = call(api, "get", "/contacts?deleted=true", workspace).json()
    assert [c["id"] for c in gone["items"]] == [made["id"]]
    restored = call(api, "post", f"/contacts/{made['id']}/restore", workspace)
    assert restored.status_code == 200 and restored.json()["deleted_at"] is None


def test_validation_and_conflict_responses(api, workspace):
    bad = call(api, "post", "/contacts", workspace, {"email": "nope", "country": "Canada"})
    assert bad.status_code == 422
    assert {e["loc"][0] for e in bad.json()["errors"]} == {"email", "country"}
    create(api, workspace, email="a@example.com")
    dup = call(api, "post", "/contacts", workspace, {"email": "A@example.com"})
    assert dup.status_code == 409
    assert dup.json()["errors"] == [{"loc": ["email"], "type": "duplicate"}]


def test_upsert_reports_whether_it_created(api, workspace):
    first = call(
        api, "post", "/contacts/upsert", workspace, {"email": "a@example.com", "first_name": "Ada"}
    )
    second = call(
        api, "post", "/contacts/upsert", workspace, {"email": "a@example.com", "city": "Ottawa"}
    )
    assert first.json()["created"] is True and second.json()["created"] is False
    assert (
        second.json()["contact"]["first_name"] == "Ada"
        and second.json()["contact"]["city"] == "Ottawa"
    )


def test_list_search_filters_and_pagination(api, workspace):
    for i in range(5):
        create(
            api,
            workspace,
            first_name=f"Person{i}",
            email=f"p{i}@example.com",
            source="web" if i % 2 else "ads",
        )
    create(api, workspace, first_name="Zed", email="zed@acme.test", tags=["hot"])
    everything = call(api, "get", "/contacts?limit=4", workspace).json()
    assert len(everything["items"]) == 4 and everything["next_cursor"]
    rest = call(
        api, "get", f"/contacts?limit=4&cursor={everything['next_cursor']}", workspace
    ).json()
    assert len(rest["items"]) == 2 and rest["next_cursor"] is None
    assert [
        c["first_name"] for c in call(api, "get", "/contacts?q=zed acme", workspace).json()["items"]
    ] == ["Zed"]
    assert len(call(api, "get", "/contacts?source=web", workspace).json()["items"]) == 2
    assert [
        c["first_name"] for c in call(api, "get", "/contacts?tag=HOT", workspace).json()["items"]
    ] == ["Zed"]
    assert call(api, "get", "/contacts?created_after=not-a-date", workspace).status_code == 422


def test_merge_and_undo_through_the_api(api, workspace):
    winner = create(api, workspace, email="w@example.com", first_name="Ada")
    loser = create(api, workspace, email="l@example.com", city="Toronto")
    merged = call(
        api,
        "post",
        "/contacts/merge",
        workspace,
        {"winner_id": winner["id"], "loser_id": loser["id"]},
    )
    assert merged.status_code == 201
    got = call(api, "get", f"/contacts/{winner['id']}", workspace).json()
    assert got["city"] == "Toronto" and got["extra_emails"] == ["l@example.com"]
    assert call(api, "get", f"/contacts/{loser['id']}", workspace).status_code == 404
    undone = call(api, "post", f"/contacts/merges/{merged.json()['id']}/undo", workspace)
    assert undone.status_code == 200 and undone.json()["undone_at"]
    assert (
        call(api, "get", f"/contacts/{loser['id']}", workspace).json()["email"] == "l@example.com"
    )


def test_tags_and_companies(api, workspace):
    c = create(api, workspace, email="a@example.com")
    added = call(api, "post", f"/contacts/{c['id']}/tags", workspace, {"names": ["a", "B"]})
    assert added.json()["tags"] == ["a", "B"]
    removed = call(api, "post", f"/contacts/{c['id']}/tags/remove", workspace, {"names": ["a"]})
    assert removed.json()["tags"] == ["B"]
    tag = call(api, "get", "/tags", workspace).json()[0]
    assert (
        call(api, "patch", f"/tags/{tag['id']}", workspace, {"name": "Bee"}).json()["name"] == "Bee"
    )
    company = call(api, "post", "/companies", workspace, {"name": "Acme"})
    assert company.status_code == 201
    linked = call(
        api, "patch", f"/contacts/{c['id']}", workspace, {"company_id": company.json()["id"]}
    )
    assert linked.json()["company_id"] == company.json()["id"]
    assert call(api, "get", "/companies?q=acm", workspace).json()["items"][0]["name"] == "Acme"
    assert call(api, "delete", f"/companies/{company.json()['id']}", workspace).status_code == 204


def test_settings_roundtrip(api, workspace):
    assert call(api, "get", "/settings", workspace).json() == {"match_order": "email_first"}
    assert (
        call(api, "put", "/settings", workspace, {"match_order": "phone_first"}).json()[
            "match_order"
        ]
        == "phone_first"
    )
    assert call(api, "put", "/settings", workspace, {"match_order": "bogus"}).status_code == 422


# ---- permissions -----------------------------------------------------------------------------


def test_role_permissions(workspace, api):
    contact = create(api, workspace, email="a@example.com")
    viewer = login(make_user("v@example.com", workspace, Role.VIEWER))
    editor = login(make_user("e@example.com", workspace, Role.EDITOR))
    assert call(viewer, "get", "/contacts", workspace).status_code == 200
    assert (
        call(viewer, "post", "/contacts", workspace, {"email": "x@example.com"}).status_code == 403
    )
    assert (
        call(editor, "post", "/contacts", workspace, {"email": "x@example.com"}).status_code == 201
    )
    assert call(editor, "delete", f"/contacts/{contact['id']}", workspace).status_code == 403
    assert call(editor, "post", f"/contacts/{contact['id']}/erase", workspace).status_code == 403
    assert call(api, "post", f"/contacts/{contact['id']}/erase", workspace).status_code == 204


def test_assigned_scope_hides_other_contacts(workspace, api, owner):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    workspace_services.set_record_scope(
        workspace=workspace, user=rep, scope=RecordScope.ASSIGNED, changed_by=owner
    )
    mine = create(api, workspace, email="mine@example.com", assigned_to=str(rep.pk))
    theirs = create(api, workspace, email="theirs@example.com")
    client = login(rep)
    listed = call(client, "get", "/contacts", workspace).json()["items"]
    assert [c["id"] for c in listed] == [mine["id"]]
    assert call(client, "get", f"/contacts/{mine['id']}", workspace).status_code == 200
    for method, path in (("get", ""), ("patch", ""), ("post", "/assign")):
        body = {"user_id": None} if path else ({"city": "x"} if method == "patch" else None)
        assert (
            call(client, method, f"/contacts/{theirs['id']}{path}", workspace, body).status_code
            == 404
        )
    merge = call(
        client,
        "post",
        "/contacts/merge",
        workspace,
        {"winner_id": mine["id"], "loser_id": theirs["id"]},
    )
    assert merge.status_code == 404


# ---- cross-tenant -----------------------------------------------------------------------------


def test_nothing_leaks_across_workspaces(workspace, api, owner):
    other = create_workspace(name="Other Co", owner=owner)
    contact = create(api, workspace, email="secret@example.com", tags=["x"])
    company = call(api, "post", "/companies", workspace, {"name": "Acme"}).json()
    tag = call(api, "get", "/tags", workspace).json()[0]
    assert call(api, "get", "/contacts", other).json()["items"] == []
    assert call(api, "get", "/tags", other).json() == []
    probes = [
        ("get", f"/contacts/{contact['id']}", None),
        ("patch", f"/contacts/{contact['id']}", {"city": "x"}),
        ("delete", f"/contacts/{contact['id']}", None),
        ("post", f"/contacts/{contact['id']}/restore", None),
        ("post", f"/contacts/{contact['id']}/erase", None),
        ("post", f"/contacts/{contact['id']}/tags", {"names": ["y"]}),
        ("get", f"/companies/{company['id']}", None),
        ("patch", f"/companies/{company['id']}", {"name": "hijack"}),
        ("delete", f"/companies/{company['id']}", None),
        ("patch", f"/tags/{tag['id']}", {"name": "hijack"}),
    ]
    for method, path, body in probes:
        status = call(api, method, path, other, body).status_code
        assert status == 404, f"{method} {path} -> {status}"
    other_contact = create(api, other, email="own@example.com")
    cross = call(
        api,
        "post",
        "/contacts/merge",
        other,
        {"winner_id": other_contact["id"], "loser_id": contact["id"]},
    )
    assert cross.status_code == 404
    assert (
        call(
            api, "patch", f"/contacts/{other_contact['id']}", other, {"company_id": company["id"]}
        ).status_code
        == 422
    )
    assert (
        call(api, "get", f"/contacts/{contact['id']}", workspace).json()["email"]
        == "secret@example.com"
    )


def test_unauthenticated_requests_are_rejected(workspace):
    assert (
        Client().get(f"{BASE}/contacts", HTTP_X_WORKSPACE_ID=str(workspace.pk)).status_code == 401
    )


# ---- bulk actions ----------------------------------------------------------------------------


def test_bulk_tags_assign_and_delete(api, workspace, owner):
    first = create(api, workspace, first_name="Ada", email="ada@example.com")
    second = create(api, workspace, first_name="Grace", email="grace@example.com")
    ids = [first["id"], second["id"]]
    path = "/contacts/bulk"

    out = call(api, "post", path, workspace, {"ids": ids, "action": "add_tags", "names": ["vip"]})
    assert out.json() == {"done": 2, "skipped": 0}
    assert call(api, "get", f"/contacts/{first['id']}", workspace).json()["tags"] == ["vip"]
    call(api, "post", path, workspace, {"ids": ids, "action": "remove_tags", "names": ["vip"]})
    assert call(api, "get", f"/contacts/{second['id']}", workspace).json()["tags"] == []

    call(api, "post", path, workspace, {"ids": ids, "action": "assign", "user_id": str(owner.pk)})
    assert call(api, "get", f"/contacts/{first['id']}", workspace).json()["assigned_to"] == str(
        owner.pk
    )
    call(api, "post", path, workspace, {"ids": ids, "action": "unassign"})
    assert call(api, "get", f"/contacts/{first['id']}", workspace).json()["assigned_to"] is None

    call(api, "post", path, workspace, {"ids": [first["id"]], "action": "delete"})
    assert call(api, "get", f"/contacts/{first['id']}", workspace).status_code == 404


def test_bulk_rejects_bad_requests_roles_and_other_workspaces(api, workspace, owner, settings):
    settings.CRM_CONTACTS_BULK_LIMIT = 2
    made = create(api, workspace, first_name="Ada", email="ada@example.com")
    path = "/contacts/bulk"
    one = [made["id"]]
    assert call(api, "post", path, workspace, {"ids": one, "action": "explode"}).status_code == 422
    assert call(api, "post", path, workspace, {"ids": one, "action": "assign"}).status_code == 422
    assert call(api, "post", path, workspace, {"ids": one, "action": "add_tags"}).status_code == 422
    many = {"ids": [made["id"]] + [str(uuid.uuid4()) for _ in range(2)], "action": "unassign"}
    assert call(api, "post", path, workspace, many).status_code == 422

    other = create_workspace(name="Other Co", owner=owner)
    leaked = call(api, "post", path, other, {"ids": one, "action": "delete"})
    assert leaked.json() == {"done": 0, "skipped": 1}
    assert call(api, "get", f"/contacts/{made['id']}", workspace).status_code == 200

    editor = make_user("editor@example.com", workspace, Role.EDITOR)
    denied = call(login(editor), "post", path, workspace, {"ids": one, "action": "delete"})
    assert denied.status_code == 403
    ok = call(login(editor), "post", path, workspace, {"ids": one, "action": "unassign"})
    assert ok.status_code == 200
