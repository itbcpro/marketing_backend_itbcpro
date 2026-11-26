import json
import uuid
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.outbox import dispatch_batch
from conftest import make_user
from core.errors import PermissionDeniedError, ValidationFailedError
from crm.activity import services
from crm.activity.models import Activity
from crm.consent import services as consent
from crm.contacts import services as contacts
from workspaces import services as workspace_services
from workspaces.models import RecordScope, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


def contact(workspace, owner=None, **data):
    return contacts.create_contact(workspace_id=workspace.pk, actor=SYSTEM, data=data)


def human(user):
    return Actor.human(user.pk)


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def login(user):
    client = Client()
    client.force_login(user)
    return client


# ---- notes ---------------------------------------------------------------------------------


def test_notes_validate_and_keep_pinned_first(workspace, owner):
    c = contact(workspace)
    first = services.add_note(
        workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, body=" hello "
    )
    pinned = services.add_note(
        workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, body="x", pinned=True
    )
    assert first.body == "hello" and first.author_id == owner.pk
    assert [n.pk for n in c.notes.all()] == [pinned.pk, first.pk]
    with pytest.raises(ValidationFailedError):
        services.add_note(
            workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, body="   "
        )
    with pytest.raises(ValidationFailedError):
        services.add_note(
            workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, body="x" * 20_001
        )


def test_mentions_must_be_members(workspace, owner):
    c = contact(workspace)
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    note = services.add_note(
        workspace_id=workspace.pk,
        actor=human(owner),
        contact_id=c.pk,
        body="ping",
        mentions=[rep.pk],
    )
    assert note.mentions == [str(rep.pk)]
    with pytest.raises(ValidationFailedError):
        services.add_note(
            workspace_id=workspace.pk,
            actor=human(owner),
            contact_id=c.pk,
            body="ping",
            mentions=[uuid.uuid4()],
        )


def test_only_authors_edit_and_delete_notes_unless_allowed(workspace, owner):
    c = contact(workspace)
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    note = services.add_note(
        workspace_id=workspace.pk, actor=human(rep), contact_id=c.pk, body="mine"
    )
    with pytest.raises(PermissionDeniedError):
        services.update_note(
            workspace_id=workspace.pk, actor=human(owner), note_id=note.pk, body="changed"
        )
    with pytest.raises(PermissionDeniedError):
        services.delete_note(workspace_id=workspace.pk, actor=human(owner), note_id=note.pk)
    edited = services.update_note(
        workspace_id=workspace.pk, actor=human(rep), note_id=note.pk, body="better"
    )
    assert edited.edited_at is not None
    services.update_note(
        workspace_id=workspace.pk, actor=human(owner), note_id=note.pk, pinned=True
    )  # pin: anyone
    services.delete_note(
        workspace_id=workspace.pk, actor=human(owner), note_id=note.pk, can_delete_others=True
    )


# ---- tasks ---------------------------------------------------------------------------------


def test_task_lifecycle(workspace, owner):
    c = contact(workspace)
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    task = services.create_task(
        workspace_id=workspace.pk,
        actor=human(owner),
        contact_id=c.pk,
        title=" Call back ",
        assigned_to=rep.pk,
    )
    assert task.title == "Call back" and task.status == "incomplete"
    done = services.complete_task(workspace_id=workspace.pk, actor=human(rep), task_id=task.pk)
    assert done.status == "complete" and done.completed_by_id == rep.pk and done.completed_at
    assert (
        services.complete_task(
            workspace_id=workspace.pk, actor=human(rep), task_id=task.pk
        ).completed_at
        == done.completed_at
    )
    reopened = services.reopen_task(workspace_id=workspace.pk, actor=human(rep), task_id=task.pk)
    assert reopened.status == "incomplete" and reopened.completed_at is None
    services.delete_task(workspace_id=workspace.pk, actor=human(rep), task_id=task.pk)


def test_task_validation(workspace, owner):
    c = contact(workspace)
    for bad in ("", " ", "x" * 201):
        with pytest.raises(ValidationFailedError):
            services.create_task(
                workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, title=bad
            )
    with pytest.raises(ValidationFailedError):
        services.create_task(
            workspace_id=workspace.pk,
            actor=human(owner),
            contact_id=c.pk,
            title="ok",
            assigned_to=uuid.uuid4(),
        )


# ---- timeline ------------------------------------------------------------------------------


def test_timeline_is_built_from_events_once(workspace, owner):
    c = contact(workspace, email="a@example.com")
    contacts.add_tags(workspace_id=workspace.pk, actor=SYSTEM, contact=c, names=["vip"])
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=c.pk,
        channel="email",
        purpose="marketing",
        status="opted_in",
        source="form",
    )
    services.add_note(workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, body="hi")
    task = services.create_task(
        workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, title="t"
    )
    services.complete_task(workspace_id=workspace.pk, actor=human(owner), task_id=task.pk)
    dispatch_batch()
    kinds = sorted(Activity.objects.filter(contact=c).values_list("kind", flat=True))
    assert kinds == sorted(
        [
            "contact.created",
            "tag.added",
            "consent.changed",
            "note.added",
            "task.created",
            "task.completed",
        ]
    )
    dispatch_batch()  # nothing left, and redelivery would not duplicate
    assert Activity.objects.filter(contact=c).count() == 6


def test_timeline_survives_merge_and_skips_erased_contacts(workspace, owner):
    winner, loser = contact(workspace), contact(workspace)
    contacts.merge_contacts(
        workspace_id=workspace.pk, actor=SYSTEM, winner_id=winner.pk, loser_id=loser.pk
    )
    gone = contact(workspace)
    contacts.erase_contact(workspace_id=workspace.pk, actor=SYSTEM, contact_id=gone.pk)
    dispatch_batch()
    assert Activity.objects.filter(contact=winner, kind="contact.merged").exists()
    assert not Activity.objects.filter(contact_id=gone.pk).exists()


def test_activities_are_append_only(workspace):
    c = contact(workspace)
    dispatch_batch()
    row = Activity.objects.filter(contact=c).first()
    row.kind = "x"
    with pytest.raises(RuntimeError):
        row.save()


# ---- API -----------------------------------------------------------------------------------


def test_api_notes_tasks_timeline(workspace, owner):
    admin = login(owner)
    c = contact(workspace)
    note = api(admin, "post", f"/contacts/{c.pk}/notes", workspace, {"body": "first"})
    assert note.status_code == 201
    assert api(admin, "get", f"/contacts/{c.pk}/notes", workspace).json()[0]["body"] == "first"
    patched = api(admin, "patch", f"/notes/{note.json()['id']}", workspace, {"pinned": True})
    assert patched.json()["pinned"] is True
    due = (timezone.now() + timedelta(days=1)).isoformat()
    task = api(
        admin,
        "post",
        f"/contacts/{c.pk}/tasks",
        workspace,
        {"title": "Call", "due_at": due, "assigned_to": str(owner.pk)},
    )
    assert task.status_code == 201
    assert len(api(admin, "get", "/tasks?scope=mine", workspace).json()["items"]) == 1
    assert api(admin, "get", "/tasks?scope=overdue", workspace).json()["items"] == []
    assert api(admin, "get", "/tasks?scope=nope", workspace).status_code == 422
    done = api(admin, "post", f"/tasks/{task.json()['id']}/complete", workspace)
    assert done.json()["status"] == "complete"
    assert api(admin, "get", "/tasks?scope=open", workspace).json()["items"] == []
    assert len(api(admin, "get", "/tasks?scope=all", workspace).json()["items"]) == 1
    dispatch_batch()
    page = api(admin, "get", f"/contacts/{c.pk}/timeline?limit=2", workspace).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = api(
        admin, "get", f"/contacts/{c.pk}/timeline?limit=50&cursor={page['next_cursor']}", workspace
    ).json()
    ids = [i["id"] for i in page["items"] + rest["items"]]
    assert len(ids) == len(set(ids)) >= 4


def test_api_permissions_and_scope(workspace, owner):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    workspace_services.set_record_scope(
        workspace=workspace, user=rep, scope=RecordScope.ASSIGNED, changed_by=owner
    )
    mine = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"assigned_to": rep.pk}
    )
    theirs = contact(workspace)
    mine_task = services.create_task(
        workspace_id=workspace.pk, actor=SYSTEM, contact_id=mine.pk, title="m"
    )
    their_task = services.create_task(
        workspace_id=workspace.pk, actor=SYSTEM, contact_id=theirs.pk, title="t"
    )
    rep_client, viewer_client = login(rep), login(viewer)
    assert (
        api(
            viewer_client, "post", f"/contacts/{mine.pk}/notes", workspace, {"body": "x"}
        ).status_code
        == 403
    )
    assert [
        t["id"] for t in api(rep_client, "get", "/tasks?scope=all", workspace).json()["items"]
    ] == [str(mine_task.pk)]
    assert api(rep_client, "post", f"/tasks/{their_task.pk}/complete", workspace).status_code == 404
    assert api(rep_client, "get", f"/contacts/{theirs.pk}/timeline", workspace).status_code == 404
    assert api(rep_client, "get", f"/contacts/{theirs.pk}/notes", workspace).status_code == 404
    assert (
        api(
            rep_client, "post", f"/contacts/{theirs.pk}/tasks", workspace, {"title": "x"}
        ).status_code
        == 404
    )


def test_nothing_leaks_across_workspaces(workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    admin = login(owner)
    c = contact(workspace)
    note = services.add_note(
        workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, body="secret"
    )
    task = services.create_task(
        workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, title="secret"
    )
    dispatch_batch()
    assert api(admin, "get", "/tasks?scope=all", other).json()["items"] == []
    probes = [
        ("get", f"/contacts/{c.pk}/timeline", None),
        ("get", f"/contacts/{c.pk}/notes", None),
        ("post", f"/contacts/{c.pk}/notes", {"body": "x"}),
        ("patch", f"/notes/{note.pk}", {"pinned": True}),
        ("delete", f"/notes/{note.pk}", None),
        ("post", f"/tasks/{task.pk}/complete", None),
        ("post", f"/tasks/{task.pk}/reopen", None),
        ("patch", f"/tasks/{task.pk}", {"title": "x"}),
        ("delete", f"/tasks/{task.pk}", None),
    ]
    for method, path, body in probes:
        status = api(admin, method, path, other, body).status_code
        assert status == 404, f"{method} {path} -> {status}"


# ---- notes about a deal ---------------------------------------------------------------------


def deal_of(workspace, c):
    from crm.pipelines import services as pipelines

    pipeline = pipelines.create_pipeline(workspace.pk, name="Sales", stages=[("New", 10)])
    return pipelines.create_opportunity(
        workspace.pk,
        SYSTEM,
        contact_id=c.pk,
        pipeline_id=pipeline.pk,
        name="Big deal",
    )


def test_deal_notes_are_listed_apart_from_contact_notes(workspace, owner):
    c = contact(workspace)
    deal = deal_of(workspace, c)
    services.add_note(workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, body="plain")
    services.add_note(
        workspace_id=workspace.pk,
        actor=human(owner),
        contact_id=c.pk,
        body="about the deal",
        opportunity_id=deal.pk,
    )
    admin = login(owner)
    every = api(admin, "get", f"/contacts/{c.pk}/notes", workspace).json()
    only = api(admin, "get", f"/contacts/{c.pk}/notes?opportunity_id={deal.pk}", workspace).json()
    assert len(every) == 2
    assert [n["body"] for n in only] == ["about the deal"]
    assert only[0]["opportunity_id"] == str(deal.pk)


def test_a_note_cannot_point_at_another_contacts_or_workspaces_deal(workspace, owner):
    c, other_contact = contact(workspace), contact(workspace)
    deal = deal_of(workspace, other_contact)
    with pytest.raises(ValidationFailedError):
        services.add_note(
            workspace_id=workspace.pk,
            actor=human(owner),
            contact_id=c.pk,
            body="x",
            opportunity_id=deal.pk,
        )
    other = create_workspace(name="Other Co", owner=owner)
    admin = login(owner)
    own = contact(other)
    status = api(
        admin,
        "post",
        f"/contacts/{own.pk}/notes",
        other,
        {"body": "x", "opportunity_id": str(deal.pk)},
    ).status_code
    assert status == 404


def test_task_list_names_the_contact(workspace, owner):
    c = contact(workspace, first_name="Ada", last_name="Lovelace")
    services.create_task(
        workspace_id=workspace.pk, actor=human(owner), contact_id=c.pk, title="Call"
    )
    rows = api(login(owner), "get", "/tasks?scope=open", workspace).json()["items"]
    assert [r["contact_name"] for r in rows] == ["Ada Lovelace"]
