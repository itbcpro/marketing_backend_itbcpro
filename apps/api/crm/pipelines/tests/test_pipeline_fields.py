"""New deal and stage data: close date, priority, tags, probability override, entry rules."""

from datetime import date, timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.contacts import services as contacts
from crm.pipelines import rules, services
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def pipeline(workspace):
    return services.create_pipeline(
        workspace.pk, name="Sales", stages=[("New", 10), ("Proposal", 50), ("Closing", 90)]
    )


@pytest.fixture
def person(workspace):
    return contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"first_name": "Ada", "email": "a@x.test"}
    )


def make(workspace, pipeline, person, **kwargs):
    return services.create_opportunity(
        workspace.pk,
        SYSTEM,
        contact_id=person.pk,
        pipeline_id=pipeline.pk,
        name=kwargs.pop("name", "Deal"),
        **kwargs,
    )


def test_new_fields_round_trip_and_defaults(workspace, pipeline, person):
    plain = make(workspace, pipeline, person)
    assert (plain.priority, plain.tags, plain.expected_close_date) == ("normal", [], None)
    d = make(
        workspace,
        pipeline,
        person,
        expected_close_date=date(2027, 1, 31),
        priority="high",
        tags=["  Renewal ", "renewal", "VIP"],
        probability_override=65,
    )
    assert d.tags == ["Renewal", "VIP"]
    assert (d.priority, d.probability_override) == ("high", 65)
    d = services.update_opportunity(workspace.pk, SYSTEM, d, expected_close_date=None)
    assert d.expected_close_date is None
    d = services.update_opportunity(workspace.pk, SYSTEM, d, priority=None, name="Renamed")
    assert (d.priority, d.name) == ("high", "Renamed")


@pytest.mark.parametrize(
    "bad",
    [
        {"priority": "urgent"},
        {"tags": [str(n) for n in range(11)]},
        {"tags": ["x" * 31]},
        {"probability_override": 101},
        {"currency": "DOLLARS"},
    ],
)
def test_bad_values_are_refused(workspace, pipeline, person, bad):
    with pytest.raises(ValidationFailedError):
        make(workspace, pipeline, person, **bad)
    d = make(workspace, pipeline, person)
    with pytest.raises(ValidationFailedError):
        services.update_opportunity(workspace.pk, SYSTEM, d, **bad)


def test_unknown_field_is_refused(workspace, pipeline, person):
    d = make(workspace, pipeline, person)
    with pytest.raises(ValidationFailedError):
        services.update_opportunity(workspace.pk, SYSTEM, d, status="won")
    with pytest.raises(ValidationFailedError):
        services.update_stage(workspace.pk, pipeline.stages.first().pk, position=9)


def test_stage_requirements_block_a_move_until_met(workspace, pipeline, person):
    _, proposal, _ = list(pipeline.stages.all())
    services.update_stage(
        workspace.pk, proposal.pk, required_fields=["value", "expected_close_date", "owner"]
    )
    d = make(workspace, pipeline, person)
    with pytest.raises(ValidationFailedError) as refused:
        services.move_opportunity(workspace.pk, SYSTEM, d, proposal.pk)
    assert set(refused.value.params["errors"]) == {"value", "expected_close_date", "owner"}
    assert services.bulk_move(workspace.pk, SYSTEM, [d.pk], proposal.pk) == 0
    services.update_opportunity(
        workspace.pk, SYSTEM, d, value_micros=5, expected_close_date=date(2027, 2, 1)
    )
    services.assign_opportunity(workspace.pk, SYSTEM, d, None)
    with pytest.raises(ValidationFailedError):
        services.move_opportunity(workspace.pk, SYSTEM, d, proposal.pk)


def test_requirements_are_listed_for_the_stage_editor(workspace, owner):
    client = Client()
    client.force_login(owner)
    found = client.get("/api/v1/crm/stage-requirements", HTTP_X_WORKSPACE_ID=str(workspace.pk))
    assert found.status_code == 200 and sorted(found.json()) == sorted(rules.available())


def test_requirements_must_be_known(workspace, pipeline):
    stage = pipeline.stages.first()
    with pytest.raises(ValidationFailedError):
        services.update_stage(workspace.pk, stage.pk, required_fields=["telepathy"])
    assert sorted(rules.available()) == ["expected_close_date", "owner", "value"]


def test_deal_probability_overrides_its_stage_in_the_forecast(workspace, pipeline, person):
    first = pipeline.stages.first()
    make(workspace, pipeline, person, value_micros=1_000_000)
    make(
        workspace,
        pipeline,
        person,
        name="Sure thing",
        value_micros=1_000_000,
        probability_override=100,
    )
    totals = {t.stage_id: t for t in services.stage_totals(workspace.pk, pipeline.pk)}
    assert totals[first.pk].count == 2
    assert totals[first.pk].value_micros == 2_000_000
    assert totals[first.pk].weighted_micros == 100_000 + 1_000_000


def test_stage_threshold_overrides_the_default_for_staleness(workspace, pipeline, person, settings):
    settings.CRM_STALE_DEAL_DAYS = 30
    first, proposal, _ = list(pipeline.stages.all())
    quick = make(workspace, pipeline, person, name="Quick", stage_id=first.pk)
    slow = make(workspace, pipeline, person, name="Slow", stage_id=proposal.pk)
    when = timezone.now() - timedelta(days=5)
    for d in (quick, slow):
        d.last_stage_change_at = when
        d.save(update_fields=["last_stage_change_at"])
    assert list(services.stale_opportunities(workspace.pk)) == []
    services.update_stage(workspace.pk, first.pk, rotting_days=3)
    assert [d.pk for d in services.stale_opportunities(workspace.pk)] == [quick.pk]
    services.update_stage(workspace.pk, first.pk, rotting_days=None)
    assert list(services.stale_opportunities(workspace.pk)) == []
    with pytest.raises(ValidationFailedError):
        services.update_stage(workspace.pk, first.pk, rotting_days=0)


def test_other_workspaces_cannot_touch_the_stage(workspace, pipeline, owner):
    other = create_workspace(name="Other Co", owner=owner)
    stage = pipeline.stages.first()
    with pytest.raises(NotFoundError):
        services.update_stage(other.pk, stage.pk, name="Hacked")
    with pytest.raises(NotFoundError):
        services.add_stage(other.pk, pipeline.pk, name="Sneaky")


def test_duplicate_stage_name_still_conflicts(workspace, pipeline):
    stage = pipeline.stages.first()
    with pytest.raises(ConflictError):
        services.update_stage(workspace.pk, stage.pk, name="Proposal")
