import json
import uuid
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.outbox import dispatch_batch
from conftest import make_user
from crm.activity import appointments
from crm.activity.models import Activity, Appointment
from crm.contacts import services as contacts
from crm.pipelines import services as pipelines
from workspaces.models import Membership, RecordScope, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


def contact(workspace, **data):
    return contacts.create_contact(workspace_id=workspace.pk, actor=SYSTEM, data=data)


def deal_of(workspace, c):
    pipeline = pipelines.create_pipeline(workspace.pk, name="Sales", stages=[("New", 10)])
    return pipelines.create_opportunity(
        workspace.pk, SYSTEM, contact_id=c.pk, pipeline_id=pipeline.pk, name="Big deal"
    )


def call(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def login(user):
    client = Client()
    client.force_login(user)
    return client


def slot(hours=24, length=1):
    start = timezone.now() + timedelta(hours=hours)
    return start, start + timedelta(hours=length)


def body(**over):
    start, end = slot()
    return {
        "title": "Intro call",
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
    } | over


def test_book_update_status_and_delete_through_the_api(workspace, owner):
    admin = login(owner)
    c = contact(workspace, first_name="Ada", last_name="Lovelace")
    deal = deal_of(workspace, c)
    made = call(
        admin,
        "post",
        f"/contacts/{c.pk}/appointments",
        workspace,
        body(location="Zoom", opportunity_id=str(deal.pk), assigned_to=str(owner.pk)),
    )
    assert made.status_code == 201, made.content
    row = made.json()
    assert row["contact_name"] == "Ada Lovelace"
    assert row["status"] == "scheduled"
    assert row["opportunity_id"] == str(deal.pk)

    path = f"/appointments/{row['id']}"
    edited = call(
        admin, "patch", path, workspace, {"title": "Kick-off", "notes": "Bring the quote"}
    )
    assert edited.json()["title"] == "Kick-off"
    done = call(admin, "post", f"{path}/status", workspace, {"status": "completed"})
    assert done.json()["status"] == "completed"
    assert call(admin, "post", f"{path}/status", workspace, {"status": "nope"}).status_code == 422

    dispatch_batch()
    kinds = set(Activity.objects.filter(contact=c).values_list("kind", flat=True))
    assert {"appointment.booked", "appointment.status_changed"} <= kinds

    assert call(admin, "delete", path, workspace).status_code == 204
    assert not Appointment.objects.filter(pk=row["id"]).exists()


def test_list_filters_by_contact_deal_and_time(workspace, owner):
    admin = login(owner)
    c, other = contact(workspace, first_name="Ada"), contact(workspace, first_name="Grace")
    deal = deal_of(workspace, c)
    plans = [
        (c, slot(2), {"opportunity_id": str(deal.pk)}),
        (c, slot(48), {}),
        (c, slot(-48), {}),
        (other, slot(2), {}),
    ]
    for who, window, extra in plans:
        data = body(starts_at=window[0].isoformat(), ends_at=window[1].isoformat(), **extra)
        made = call(admin, "post", f"/contacts/{who.pk}/appointments", workspace, data)
        assert made.status_code == 201

    def count(query):
        return len(call(admin, "get", f"/appointments?{query}", workspace).json())

    assert count(f"contact_id={c.pk}") == 2  # upcoming only by default
    assert count(f"contact_id={c.pk}&scope=all") == 3
    assert count(f"contact_id={c.pk}&scope=past") == 1
    assert count(f"opportunity_id={deal.pk}") == 1
    assert call(admin, "get", "/appointments?scope=bogus", workspace).status_code == 422


def test_validation(workspace, owner):
    c = contact(workspace, first_name="Ada")
    other_contact = contact(workspace, first_name="Grace")
    deal = deal_of(workspace, other_contact)
    admin = login(owner)
    path = f"/contacts/{c.pk}/appointments"
    start, end = slot()
    naive = {
        "starts_at": start.replace(tzinfo=None).isoformat(),
        "ends_at": end.replace(tzinfo=None).isoformat(),
    }
    cases = [
        body(title="  "),
        body(starts_at=end.isoformat(), ends_at=start.isoformat()),
        body(**naive),
        body(title="x" * 201),
        body(assigned_to=str(uuid.uuid4())),
        body(opportunity_id=str(deal.pk)),
    ]
    for data in cases:
        assert call(admin, "post", path, workspace, data).status_code == 422, data


def test_nothing_leaks_across_workspaces_and_roles(workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    admin = login(owner)
    c = contact(workspace, first_name="Ada")
    row = call(admin, "post", f"/contacts/{c.pk}/appointments", workspace, body()).json()
    path = f"/appointments/{row['id']}"
    assert call(admin, "get", "/appointments?scope=all", other).json() == []
    probes = [
        ("post", f"/contacts/{c.pk}/appointments", body()),
        ("patch", path, {"title": "x"}),
        ("post", f"{path}/status", {"status": "completed"}),
        ("delete", path, None),
    ]
    for method, url, payload in probes:
        assert call(admin, method, url, other, payload).status_code == 404, (method, url)

    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    reader = login(viewer)
    assert call(reader, "get", "/appointments?scope=all", workspace).status_code == 200
    denied = call(reader, "post", f"/contacts/{c.pk}/appointments", workspace, body())
    assert denied.status_code == 403
    assert call(reader, "delete", path, workspace).status_code == 403


def test_assigned_scope_only_sees_own_appointments(workspace, owner):
    c = contact(workspace, first_name="Ada")
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    Membership.objects.filter(workspace=workspace, user=rep).update(
        record_scope=RecordScope.ASSIGNED
    )
    start, end = slot()
    appointments.book(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=c.pk,
        title="Not yours",
        starts_at=start,
        ends_at=end,
    )
    mine = appointments.book(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=c.pk,
        title="Yours",
        starts_at=start,
        ends_at=end,
        assigned_to=rep.pk,
    )
    rows = call(login(rep), "get", "/appointments?scope=all", workspace).json()
    assert [r["id"] for r in rows] == [str(mine.pk)]
