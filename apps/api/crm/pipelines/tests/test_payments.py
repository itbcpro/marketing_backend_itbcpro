import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.outbox import dispatch_batch
from conftest import make_user
from crm.activity.models import Activity
from crm.contacts import services as contacts
from crm.pipelines import services as pipelines
from crm.pipelines.models import Payment
from crm.privacy.export import export_contact
from workspaces.models import Membership, RecordScope, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
MICROS = 1_000_000


def make_deal(workspace, *, value=1000, name="Ada", assigned_to=None):
    contact = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"first_name": name}
    )
    pipeline = pipelines.create_pipeline(workspace.pk, name="Sales", stages=[("New", 10)])
    deal = pipelines.create_opportunity(
        workspace.pk,
        SYSTEM,
        contact_id=contact.pk,
        pipeline_id=pipeline.pk,
        name=f"{name} deal",
        value_micros=value * MICROS,
        currency="CAD",
        assigned_to=assigned_to,
    )
    return contact, deal


def call(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def login(user):
    client = Client()
    client.force_login(user)
    return client


def pay(admin, workspace, deal, amount, **extra):
    body = {"amount_micros": amount * MICROS} | extra
    return call(admin, "post", f"/opportunities/{deal.pk}/payments", workspace, body)


def summary(admin, workspace, deal):
    return call(admin, "get", f"/opportunities/{deal.pk}/payments", workspace).json()["summary"]


def test_payments_and_refunds_add_up_for_the_deal(workspace, owner):
    admin = login(owner)
    _, deal = make_deal(workspace, value=1000)
    first = pay(admin, workspace, deal, 400, method="bank_transfer", reference="INV-1")
    assert first.status_code == 201, first.content
    assert first.json()["status"] == "paid"
    pay(admin, workspace, deal, 100, status="pending")
    assert pay(admin, workspace, deal, 50, kind="refund").status_code == 201

    total = summary(admin, workspace, deal)
    assert total == {
        "currency": "CAD",
        "deal_micros": 1000 * MICROS,
        "paid_micros": 400 * MICROS,
        "refunded_micros": 50 * MICROS,
        "pending_micros": 100 * MICROS,
        "net_micros": 350 * MICROS,
        "balance_micros": 650 * MICROS,
    }
    rows = call(admin, "get", f"/opportunities/{deal.pk}/payments", workspace).json()["items"]
    assert len(rows) == 3


def test_a_refund_cannot_exceed_what_was_received(workspace, owner):
    admin = login(owner)
    _, deal = make_deal(workspace)
    pay(admin, workspace, deal, 100)
    assert pay(admin, workspace, deal, 150, kind="refund").status_code == 422
    assert pay(admin, workspace, deal, 100, kind="refund").status_code == 201
    assert pay(admin, workspace, deal, 1, kind="refund").status_code == 422
    assert pay(admin, workspace, deal, 500, kind="refund", status="pending").status_code == 201


def test_validation(workspace, owner):
    admin = login(owner)
    _, deal = make_deal(workspace)
    for bad in [
        {"amount_micros": 0},
        {"amount_micros": -5},
        {"amount_micros": 10**16},
        {"kind": "gift"},
        {"status": "lost"},
        {"method": "bitcoin"},
        {"currency": "USD"},
        {"currency": "CA"},
        {"reference": "x" * 101},
        {"occurred_at": timezone.now().replace(tzinfo=None).isoformat()},
    ]:
        body = {"amount_micros": MICROS} | bad
        response = call(admin, "post", f"/opportunities/{deal.pk}/payments", workspace, body)
        assert response.status_code == 422, bad


def test_changing_status_moves_the_totals_and_protects_refunds(workspace, owner):
    admin = login(owner)
    _, deal = make_deal(workspace)
    paid = pay(admin, workspace, deal, 100).json()
    pay(admin, workspace, deal, 60, kind="refund")

    refused = call(admin, "patch", f"/payments/{paid['id']}", workspace, {"status": "failed"})
    assert refused.status_code == 422
    assert call(admin, "delete", f"/payments/{paid['id']}", workspace).status_code == 422

    more = pay(admin, workspace, deal, 200).json()
    ok = call(admin, "patch", f"/payments/{paid['id']}", workspace, {"status": "failed"})
    assert ok.status_code == 200
    assert summary(admin, workspace, deal)["paid_micros"] == 200 * MICROS
    assert more["status"] == "paid"
    edited = call(
        admin, "patch", f"/payments/{more['id']}", workspace, {"reference": "R-9", "note": "wire"}
    )
    assert edited.json()["reference"] == "R-9"
    assert call(admin, "delete", f"/payments/{ok.json()['id']}", workspace).status_code == 204
    assert Payment.objects.filter(opportunity=deal).count() == 2


def test_recording_appears_on_the_contact_timeline(workspace, owner):
    admin = login(owner)
    contact, deal = make_deal(workspace)
    pay(admin, workspace, deal, 10)
    dispatch_batch()
    kinds = set(Activity.objects.filter(contact=contact).values_list("kind", flat=True))
    assert "payment.recorded" in kinds


def test_the_access_export_lists_payments(workspace, owner):
    admin = login(owner)
    contact, deal = make_deal(workspace)
    pay(admin, workspace, deal, 75, reference="INV-7")
    data = export_contact(workspace.pk, Actor.human(owner.pk), contact.pk)
    assert [p["reference"] for p in data["payments"]] == ["INV-7"]
    assert data["payments"][0]["amount_micros"] == 75 * MICROS


def test_nothing_leaks_across_workspaces_roles_or_scopes(workspace, owner):
    admin = login(owner)
    other = create_workspace(name="Other Co", owner=owner)
    _, deal = make_deal(workspace)
    row = pay(admin, workspace, deal, 100).json()
    path = f"/payments/{row['id']}"
    assert call(admin, "get", f"/opportunities/{deal.pk}/payments", other).status_code == 404
    assert pay(admin, other, deal, 5).status_code == 404
    assert call(admin, "patch", path, other, {"note": "x"}).status_code == 404
    assert call(admin, "delete", path, other).status_code == 404

    viewer = login(make_user("viewer@example.com", workspace, Role.VIEWER))
    assert call(viewer, "get", f"/opportunities/{deal.pk}/payments", workspace).status_code == 200
    assert pay(viewer, workspace, deal, 5).status_code == 403
    editor_user = make_user("editor@example.com", workspace, Role.EDITOR)
    editor = login(editor_user)
    assert pay(editor, workspace, deal, 5).status_code == 201
    assert call(editor, "delete", path, workspace).status_code == 403

    rep_user = make_user("rep@example.com", workspace, Role.EDITOR)
    Membership.objects.filter(workspace=workspace, user=rep_user).update(
        record_scope=RecordScope.ASSIGNED
    )
    rep = login(rep_user)
    assert call(rep, "get", f"/opportunities/{deal.pk}/payments", workspace).status_code == 404
    assert call(rep, "patch", path, workspace, {"note": "x"}).status_code == 404


def test_old_payments_sort_after_new_ones(workspace, owner):
    admin = login(owner)
    _, deal = make_deal(workspace)
    old = (timezone.now() - timedelta(days=30)).isoformat()
    pay(admin, workspace, deal, 10, occurred_at=old, reference="old")
    pay(admin, workspace, deal, 20, reference="new")
    rows = call(admin, "get", f"/opportunities/{deal.pk}/payments", workspace).json()["items"]
    assert [r["reference"] for r in rows] == ["new", "old"]
