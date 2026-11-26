"""Bulk actions, pipeline insights and the CSV export of deals."""

import csv
import io
import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.models import AuditEvent
from conftest import make_user
from core.errors import ValidationFailedError
from crm.contacts import services as contacts
from crm.pipelines import bulk, services
from crm.pipelines.models import StageChange
from workspaces import services as workspace_services
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def pipeline(workspace):
    return services.create_pipeline(
        workspace.pk, name="Sales", stages=[("New", 10), ("Proposal", 50), ("Closing", 90)]
    )


@pytest.fixture
def ada(workspace):
    return contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"first_name": "Ada", "email": "a@x.test"}
    )


def make(workspace, pipeline, contact, name, **kw):
    return services.create_opportunity(
        workspace.pk, SYSTEM, contact_id=contact.pk, pipeline_id=pipeline.pk, name=name, **kw
    )


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def logged(user):
    c = Client()
    c.force_login(user)
    return c


# ---- bulk ------------------------------------------------------------------------------------


def test_bulk_applies_and_skips_what_cannot_change(workspace, pipeline, ada):
    _, proposal, _ = list(pipeline.stages.all())
    deals = [make(workspace, pipeline, ada, f"D{n}") for n in range(3)]
    services.win(workspace.pk, SYSTEM, deals[2])
    ids = [d.pk for d in deals]
    result = bulk.apply(workspace.pk, SYSTEM, ids, "move", bulk.Params(stage_id=proposal.pk))
    assert (result.done, result.skipped) == (2, 1)
    result = bulk.apply(workspace.pk, SYSTEM, ids, "reopen", bulk.Params())
    assert result.done == 3
    bulk.apply(workspace.pk, SYSTEM, ids, "add_tag", bulk.Params(tag="vip"))
    bulk.apply(workspace.pk, SYSTEM, ids, "set_priority", bulk.Params(priority="high"))
    for d in deals:
        d.refresh_from_db()
        assert d.tags == ["vip"] and d.priority == "high"
    assert bulk.apply(workspace.pk, SYSTEM, ids, "delete", bulk.Params()).done == 3
    assert services.list_opportunities(workspace.pk).count() == 0


def test_bulk_refuses_unknown_actions_and_missing_parameters(workspace, pipeline, ada):
    d = make(workspace, pipeline, ada, "D")
    with pytest.raises(ValidationFailedError):
        bulk.apply(workspace.pk, SYSTEM, [d.pk], "explode", bulk.Params())
    with pytest.raises(ValidationFailedError):
        bulk.apply(workspace.pk, SYSTEM, [d.pk], "move", bulk.Params())


def test_bulk_api_permissions_and_tenant_isolation(workspace, owner, pipeline, ada):
    d = make(workspace, pipeline, ada, "D")
    me = logged(owner)
    body = {"deal_ids": [str(d.pk)], "action": "win"}
    assert api(me, "post", "/opportunities/bulk", workspace, body).json() == {
        "done": 1,
        "skipped": 0,
    }
    other = create_workspace(name="Other Co", owner=owner)
    d.refresh_from_db()
    services.reopen(workspace.pk, SYSTEM, d)
    seen = api(me, "post", "/opportunities/bulk", other, body).json()
    assert seen == {"done": 0, "skipped": 1}
    d.refresh_from_db()
    assert d.status == "open"
    viewer = make_user("viewer@example.com")
    workspace_services.add_member(
        workspace=workspace, user=viewer, role=Role.VIEWER, added_by=owner
    )
    assert api(logged(viewer), "post", "/opportunities/bulk", workspace, body).status_code == 403
    assert (
        api(me, "post", "/opportunities/bulk", workspace, {**body, "action": "x"}).status_code
        == 422
    )


# ---- insights --------------------------------------------------------------------------------


def test_insights_cover_forecast_funnel_losses_and_owners(workspace, owner, pipeline, ada):
    _, proposal, closing = list(pipeline.stages.all())
    reason = services.create_lost_reason(workspace.pk, "No budget")
    a = make(workspace, pipeline, ada, "A", value_micros=1_000_000, assigned_to=owner.pk)
    b = make(
        workspace,
        pipeline,
        ada,
        "B",
        value_micros=2_000_000,
        expected_close_date=timezone.localdate() + timedelta(days=40),
    )
    c = make(workspace, pipeline, ada, "C", value_micros=4_000_000, assigned_to=owner.pk)
    services.move_opportunity(workspace.pk, SYSTEM, b, proposal.pk)
    services.move_opportunity(workspace.pk, SYSTEM, c, proposal.pk)
    services.move_opportunity(workspace.pk, SYSTEM, c, closing.pk)
    services.win(workspace.pk, SYSTEM, c)
    services.lose(workspace.pk, SYSTEM, a, lost_reason_id=reason.pk)
    found = services.pipeline_insights(workspace.pk, pipeline.pk)
    assert found["totals"]["won"] == 1 and found["totals"]["lost"] == 1
    assert found["totals"]["win_rate_percent"] == 50.0
    assert [f["reached"] for f in found["funnel"]] == [3, 2, 1]
    assert found["funnel"][1]["conversion_percent"] == 66.7
    assert found["time_in_stage"][0]["deals"] == 3
    assert [(r["name"], r["count"]) for r in found["lost_reasons"]] == [("No budget", 1)]
    mine = next(o for o in found["owners"] if o["user_id"] == owner.pk)
    assert (mine["won"], mine["lost"], mine["won_value_micros"]) == (1, 1, 4_000_000)
    assert sum(f["count"] for f in found["forecast"]) == 1
    assert found["forecast"][0]["weighted_micros"] == 1_000_000


def test_funnel_ignores_deals_created_before_the_period(workspace, pipeline, ada):
    old = make(workspace, pipeline, ada, "Old")
    StageChange.objects.filter(opportunity=old).update(created_at=timezone.now())
    type(old).objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=400))
    found = services.pipeline_insights(workspace.pk, pipeline.pk)
    assert [f["reached"] for f in found["funnel"]] == [0, 0, 0]


def test_insights_api_needs_report_permission_and_hides_other_tenants(
    workspace, owner, pipeline, ada
):
    make(workspace, pipeline, ada, "A")
    me = logged(owner)
    ok = api(me, "get", f"/pipelines/{pipeline.pk}/insights", workspace)
    assert ok.status_code == 200 and len(ok.json()["funnel"]) == 3
    other = create_workspace(name="Other Co", owner=owner)
    assert api(me, "get", f"/pipelines/{pipeline.pk}/insights", other).status_code == 404
    viewer = make_user("viewer@example.com")
    workspace_services.add_member(
        workspace=workspace, user=viewer, role=Role.VIEWER, added_by=owner
    )
    assert (
        api(logged(viewer), "get", f"/pipelines/{pipeline.pk}/insights", workspace).status_code
        == 403
    )


# ---- export ----------------------------------------------------------------------------------


def test_export_is_filtered_safe_and_audited(workspace, owner, pipeline, ada):
    make(workspace, pipeline, ada, "=1+1", value_micros=2_500_000, tags=["vip"])
    make(workspace, pipeline, ada, "Plain")
    me = logged(owner)
    response = api(me, "get", "/opportunities-export.csv?q=1%2B1", workspace)
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    table = list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode())))
    assert table[0][:3] == ["id", "name", "contact"]
    assert len(table) == 2 and table[1][1] == "'=1+1" and table[1][6] == "2.5"
    assert AuditEvent.objects.filter(action="crm.opportunities.exported").exists()
    other = create_workspace(name="Other Co", owner=owner)
    empty = api(me, "get", "/opportunities-export.csv", other)
    assert len(list(csv.reader(io.StringIO(b"".join(empty.streaming_content).decode())))) == 1
    viewer = make_user("viewer@example.com")
    workspace_services.add_member(
        workspace=workspace, user=viewer, role=Role.VIEWER, added_by=owner
    )
    assert api(logged(viewer), "get", "/opportunities-export.csv", workspace).status_code == 403
