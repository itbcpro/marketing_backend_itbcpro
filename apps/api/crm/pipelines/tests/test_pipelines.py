import json
import uuid
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.models import OutboxMessage
from conftest import make_user
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.contacts import services as contacts
from crm.fields import services as fields
from crm.fields.models import FieldValue
from crm.pipelines import services
from workspaces import services as workspace_services
from workspaces.models import RecordScope, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
STAGES = [("New", 10), ("Qualified", 40), ("Proposal", 70)]


@pytest.fixture
def pipeline(workspace):
    return services.create_pipeline(workspace.pk, name="Sales", stages=STAGES)


@pytest.fixture
def person(workspace):
    return contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"first_name": "Ada", "email": "a@x.test"}
    )


def deal(workspace, pipeline, person, **kwargs):
    return services.create_opportunity(
        workspace.pk,
        SYSTEM,
        contact_id=person.pk,
        pipeline_id=pipeline.pk,
        name=kwargs.pop("name", "Big deal"),
        **kwargs,
    )


def event_types():
    return list(OutboxMessage.objects.order_by("created_at").values_list("topic", flat=True))


# ---- pipelines and stages ---------------------------------------------------------------


def test_pipeline_needs_stages_and_unique_names(workspace, pipeline):
    with pytest.raises(ValidationFailedError):
        services.create_pipeline(workspace.pk, name="Empty", stages=[])
    with pytest.raises(ConflictError):
        services.create_pipeline(workspace.pk, name="Sales", stages=STAGES)
    with pytest.raises(ValidationFailedError):
        services.create_pipeline(workspace.pk, name="Bad", stages=[("A", 101)])
    assert [s.name for s in pipeline.stages.all()] == ["New", "Qualified", "Proposal"]


def test_archived_names_are_free_again(workspace, pipeline):
    services.archive_pipeline(workspace.pk, pipeline.pk)
    assert list(services.list_pipelines(workspace.pk)) == []
    assert services.create_pipeline(workspace.pk, name="Sales", stages=STAGES).pk != pipeline.pk


def test_reorder_needs_every_stage(workspace, pipeline):
    ids = [s.pk for s in pipeline.stages.all()]
    out = services.reorder_stages(workspace.pk, pipeline.pk, list(reversed(ids)))
    assert [s.pk for s in out] == list(reversed(ids))
    with pytest.raises(ValidationFailedError):
        services.reorder_stages(workspace.pk, pipeline.pk, ids[:2])
    with pytest.raises(ValidationFailedError):
        services.reorder_stages(workspace.pk, pipeline.pk, [ids[0], ids[0], ids[1]])


def test_add_and_update_stage(workspace, pipeline):
    stage = services.add_stage(workspace.pk, pipeline.pk, name="Closing", probability=90)
    assert stage.position == 3
    services.update_stage(workspace.pk, stage.pk, probability=95, name="Close")
    stage.refresh_from_db()
    assert (stage.name, stage.probability) == ("Close", 95)
    with pytest.raises(ConflictError):
        services.update_stage(workspace.pk, stage.pk, name="New")


def test_delete_stage_moves_deals_or_refuses(workspace, pipeline, person):
    first, second, _ = list(pipeline.stages.all())
    d = deal(workspace, pipeline, person, stage_id=first.pk)
    with pytest.raises(ConflictError):
        services.delete_stage(workspace.pk, first.pk)
    services.delete_stage(workspace.pk, first.pk, move_to=second.pk)
    d.refresh_from_db()
    assert d.stage_id == second.pk
    other = services.create_pipeline(workspace.pk, name="Other", stages=[("X", 0), ("Y", 0)])
    foreign = other.stages.first()
    with pytest.raises(ConflictError):
        services.delete_stage(workspace.pk, second.pk, move_to=foreign.pk)


def test_last_stage_cannot_be_deleted(workspace):
    p = services.create_pipeline(workspace.pk, name="One", stages=[("Only", 0)])
    with pytest.raises(ConflictError):
        services.delete_stage(workspace.pk, p.stages.first().pk)


# ---- opportunities ----------------------------------------------------------------------


def test_create_starts_in_first_stage_with_history_and_event(workspace, pipeline, person):
    d = deal(workspace, pipeline, person, value_micros=5_000_000)
    assert d.stage.name == "New" and d.status == "open" and d.currency
    assert len(services.history(workspace.pk, d)) == 1
    assert "crm.opportunity.created" in event_types()


def test_create_rejects_bad_input(workspace, pipeline, person):
    other = services.create_pipeline(workspace.pk, name="Other", stages=[("X", 0)])
    with pytest.raises(ValidationFailedError):
        deal(workspace, pipeline, person, stage_id=other.stages.first().pk)
    with pytest.raises(ValidationFailedError):
        deal(workspace, pipeline, person, value_micros=-1)
    with pytest.raises(ValidationFailedError):
        deal(workspace, pipeline, person, assigned_to=uuid.uuid4())
    with pytest.raises(NotFoundError):
        services.create_opportunity(
            workspace.pk, SYSTEM, contact_id=uuid.uuid4(), pipeline_id=pipeline.pk, name="x"
        )
    services.archive_pipeline(workspace.pk, pipeline.pk)
    with pytest.raises(ConflictError):
        deal(workspace, pipeline, person)


def test_move_records_history_and_stays_in_pipeline(workspace, pipeline, person):
    d = deal(workspace, pipeline, person)
    target = pipeline.stages.get(name="Proposal")
    services.move_opportunity(workspace.pk, SYSTEM, d, target.pk)
    assert [(c.from_stage_id is not None) for c in services.history(workspace.pk, d)] == [
        False,
        True,
    ]
    assert event_types().count("crm.opportunity.stage_changed") == 1
    services.move_opportunity(workspace.pk, SYSTEM, d, target.pk)  # same stage: nothing happens
    assert event_types().count("crm.opportunity.stage_changed") == 1
    other = services.create_pipeline(workspace.pk, name="Other", stages=[("X", 0)])
    with pytest.raises(ValidationFailedError):
        services.move_opportunity(workspace.pk, SYSTEM, d, other.stages.first().pk)


def test_status_lifecycle(workspace, pipeline, person):
    d = deal(workspace, pipeline, person)
    reason = services.create_lost_reason(workspace.pk, "Too expensive")
    services.lose(workspace.pk, SYSTEM, d, lost_reason_id=reason.pk)
    assert d.status == "lost" and d.closed_at and d.lost_reason_id == reason.pk
    with pytest.raises(ConflictError):  # a closed deal cannot move
        services.move_opportunity(workspace.pk, SYSTEM, d, pipeline.stages.last().pk)
    services.reopen(workspace.pk, SYSTEM, d)
    assert d.status == "open" and d.closed_at is None and d.lost_reason_id is None
    services.win(workspace.pk, SYSTEM, d)
    assert d.status == "won"
    services.abandon(workspace.pk, SYSTEM, d)
    assert d.status == "abandoned"
    assert event_types().count("crm.opportunity.status_changed") == 4
    with pytest.raises(ValidationFailedError):
        services.lose(workspace.pk, SYSTEM, d, lost_reason_id=uuid.uuid4())


def test_totals_weight_by_stage_probability(workspace, pipeline, person):
    a = deal(workspace, pipeline, person, value_micros=1000)
    deal(workspace, pipeline, person, value_micros=500, name="B")
    services.move_opportunity(workspace.pk, SYSTEM, a, pipeline.stages.get(name="Proposal").pk)
    closed = deal(workspace, pipeline, person, value_micros=9999, name="C")
    services.win(workspace.pk, SYSTEM, closed)
    totals = {t.stage_id: t for t in services.stage_totals(workspace.pk, pipeline.pk)}
    new = totals[pipeline.stages.get(name="New").pk]
    proposal = totals[pipeline.stages.get(name="Proposal").pk]
    assert (new.count, new.value_micros, new.weighted_micros) == (1, 500, 50)
    assert (proposal.count, proposal.weighted_micros) == (1, 700)


def test_stale_deals_use_the_threshold(workspace, pipeline, person, settings):
    settings.CRM_STALE_DEAL_DAYS = 7
    old = deal(workspace, pipeline, person, name="Old")
    deal(workspace, pipeline, person, name="Fresh")
    type(old).objects.filter(pk=old.pk).update(
        last_stage_change_at=timezone.now() - timedelta(days=8)
    )
    assert [d.name for d in services.stale_opportunities(workspace.pk)] == ["Old"]
    assert services.stale_opportunities(workspace.pk, days=9).count() == 0


def test_bulk_move_skips_what_cannot_move(workspace, pipeline, person):
    a, b = deal(workspace, pipeline, person, name="A"), deal(workspace, pipeline, person, name="B")
    services.win(workspace.pk, SYSTEM, b)
    target = pipeline.stages.get(name="Qualified")
    assert services.bulk_move(workspace.pk, SYSTEM, [a.pk, b.pk, uuid.uuid4()], target.pk) == 1


def test_deals_do_not_cross_workspaces(workspace, owner, pipeline, person):
    d = deal(workspace, pipeline, person)
    other = create_workspace(name="Other Co", owner=owner)
    with pytest.raises(NotFoundError):
        services.get_opportunity(other.pk, d.pk)
    with pytest.raises(NotFoundError):
        services.get_pipeline(other.pk, pipeline.pk)
    with pytest.raises(NotFoundError):
        services.create_opportunity(
            other.pk, SYSTEM, contact_id=person.pk, pipeline_id=pipeline.pk, name="x"
        )


# ---- API --------------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def logged(user):
    c = Client()
    c.force_login(user)
    return c


def test_api_roundtrip(workspace, owner, person):
    admin = logged(owner)
    created = api(
        admin,
        "post",
        "/pipelines",
        workspace,
        {"name": "Sales", "stages": [{"name": "New", "probability": 10}, {"name": "Won it"}]},
    )
    assert created.status_code == 201
    pid = created.json()["id"]
    first, second = (s["id"] for s in created.json()["stages"])
    made = api(
        admin,
        "post",
        "/opportunities",
        workspace,
        {"contact_id": str(person.pk), "pipeline_id": pid, "name": "Deal", "value_micros": 100},
    )
    assert made.status_code == 201
    did = made.json()["id"]
    assert (
        api(admin, "post", f"/opportunities/{did}/move", workspace, {"stage_id": second}).json()[
            "stage_id"
        ]
        == second
    )
    totals = api(admin, "get", f"/pipelines/{pid}/totals", workspace).json()
    assert [t["count"] for t in totals] == [0, 1]
    assert len(api(admin, "get", f"/opportunities/{did}/history", workspace).json()) == 2
    reason = api(admin, "post", "/lost-reasons", workspace, {"name": "No budget"}).json()["id"]
    lost = api(admin, "post", f"/opportunities/{did}/lose", workspace, {"lost_reason_id": reason})
    assert lost.json()["status"] == "lost"
    assert api(admin, "post", f"/opportunities/{did}/reopen", workspace).json()["status"] == "open"
    assert (
        api(admin, "patch", f"/opportunities/{did}", workspace, {"name": "Renamed"}).json()["name"]
        == "Renamed"
    )
    moved = api(
        admin, "post", "/opportunities/bulk-move", workspace, {"deal_ids": [did], "stage_id": first}
    )
    assert moved.json() == {"moved": 1}
    assert api(admin, "get", f"/opportunities?pipeline_id={pid}&status=open", workspace).json()
    assert api(admin, "delete", f"/opportunities/{did}", workspace).status_code == 204
    assert api(admin, "get", f"/opportunities/{did}", workspace).status_code == 404


def test_api_permissions(workspace, owner, pipeline, person):
    viewer = logged(make_user("v@example.com", workspace, Role.VIEWER))
    admin = logged(owner)
    d = deal(workspace, pipeline, person)
    assert api(viewer, "get", "/pipelines", workspace).status_code == 200
    assert api(viewer, "get", f"/opportunities/{d.pk}", workspace).status_code == 200
    assert (
        api(viewer, "post", "/pipelines", workspace, {"name": "X", "stages": []}).status_code == 403
    )
    assert api(viewer, "post", f"/opportunities/{d.pk}/win", workspace).status_code == 403
    assert api(viewer, "delete", f"/opportunities/{d.pk}", workspace).status_code == 403
    assert (
        api(admin, "post", "/pipelines", workspace, {"name": "X", "stages": []}).status_code == 422
    )


def test_api_cross_tenant(workspace, owner, pipeline, person):
    admin = logged(owner)
    d = deal(workspace, pipeline, person)
    other = create_workspace(name="Other Co", owner=owner)
    stage = str(pipeline.stages.first().pk)
    for method, path, payload in (
        ("get", f"/opportunities/{d.pk}", None),
        ("patch", f"/opportunities/{d.pk}", {"name": "Z"}),
        ("delete", f"/opportunities/{d.pk}", None),
        ("get", f"/opportunities/{d.pk}/history", None),
        ("post", f"/opportunities/{d.pk}/move", {"stage_id": stage}),
        ("post", f"/opportunities/{d.pk}/win", None),
        ("post", f"/opportunities/{d.pk}/lose", {}),
        ("post", f"/opportunities/{d.pk}/abandon", None),
        ("post", f"/opportunities/{d.pk}/reopen", None),
        ("post", f"/opportunities/{d.pk}/assign", {"user_id": None}),
        ("get", f"/pipelines/{pipeline.pk}/totals", None),
        ("patch", f"/pipelines/{pipeline.pk}", {"name": "Z"}),
        ("delete", f"/pipelines/{pipeline.pk}", None),
        ("post", f"/pipelines/{pipeline.pk}/stages", {"name": "Z"}),
        ("patch", f"/stages/{stage}", {"name": "Z"}),
        ("delete", f"/stages/{stage}", None),
        (
            "post",
            "/opportunities",
            {"contact_id": str(person.pk), "pipeline_id": str(pipeline.pk), "name": "x"},
        ),
    ):
        assert api(admin, method, path, other, payload).status_code == 404, path
    assert api(admin, "get", "/opportunities", other).json() == []
    assert api(admin, "get", "/pipelines", other).json() == []


def test_assigned_scope_only_sees_own_deals(workspace, owner, pipeline, person):
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    workspace_services.set_record_scope(
        workspace=workspace, user=rep, scope=RecordScope.ASSIGNED, changed_by=owner
    )
    mine = deal(workspace, pipeline, person, name="Mine", assigned_to=rep.pk)
    theirs = deal(workspace, pipeline, person, name="Theirs")
    client = logged(rep)
    seen = api(client, "get", "/opportunities", workspace).json()
    assert [r["id"] for r in seen] == [str(mine.pk)]
    assert api(client, "get", f"/opportunities/{theirs.pk}", workspace).status_code == 404
    assert api(client, "post", f"/opportunities/{theirs.pk}/win", workspace).status_code == 404
    assert api(client, "post", f"/opportunities/{mine.pk}/win", workspace).status_code == 200


# ---- custom fields on deals ----------------------------------------------------------------


def opportunity_field(workspace, key="region", field_type="text"):
    return fields.create_definition(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        object_type="opportunity",
        key=key,
        label=key.title(),
        field_type=field_type,
    )


def test_custom_values_are_stored_updated_and_removed_with_the_deal(workspace, pipeline, person):
    opportunity_field(workspace)
    opportunity_field(workspace, "seats", "number")
    d = deal(workspace, pipeline, person, custom={"region": "West", "seats": 5})
    assert services.custom_values(workspace.pk, [d]) == {d.pk: {"region": "West", "seats": 5}}
    services.update_opportunity(workspace.pk, SYSTEM, d, custom={"region": None, "seats": 7})
    assert services.custom_values(workspace.pk, [d]) == {d.pk: {"seats": 7}}
    with pytest.raises(ValidationFailedError):
        services.update_opportunity(workspace.pk, SYSTEM, d, custom={"unknown": "x"})
    services.delete_opportunity(workspace.pk, d)
    assert FieldValue.objects.filter(record_id=d.pk).count() == 0


def test_contact_fields_do_not_apply_to_deals(workspace, pipeline, person):
    fields.create_definition(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        object_type="contact",
        key="score",
        label="Score",
        field_type="number",
    )
    with pytest.raises(ValidationFailedError):
        deal(workspace, pipeline, person, custom={"score": 1})


def test_api_custom_fields_roundtrip_and_cross_tenant(workspace, owner, pipeline, person):
    opportunity_field(workspace)
    admin = logged(owner)
    made = api(
        admin,
        "post",
        "/opportunities",
        workspace,
        {
            "contact_id": str(person.pk),
            "pipeline_id": str(pipeline.pk),
            "name": "Deal",
            "custom": {"region": "East"},
        },
    )
    assert made.status_code == 201
    assert made.json()["custom"] == {"region": "East"}
    did = made.json()["id"]
    patched = api(admin, "patch", f"/opportunities/{did}", workspace, {"custom": {"region": "N"}})
    assert patched.json()["custom"] == {"region": "N"}
    listed = api(admin, "get", "/opportunities", workspace).json()
    assert listed[0]["custom"] == {"region": "N"}
    other = create_workspace(name="Other Co", owner=owner)
    assert api(admin, "get", f"/opportunities/{did}", other).status_code == 404
    assert api(admin, "get", "/opportunities", other).json() == []
    bad = api(
        admin,
        "post",
        "/opportunities",
        other,
        {
            "contact_id": str(person.pk),
            "pipeline_id": str(pipeline.pk),
            "name": "x",
            "custom": {"region": "y"},
        },
    )
    assert bad.status_code == 404
    assert FieldValue.objects.filter(workspace_id=other.pk).count() == 0
    assert fields.get_values(other.pk, "opportunity", [uuid.UUID(did)]) == {}
