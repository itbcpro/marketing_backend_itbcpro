"""Searching, filtering, sorting and summarising deals, through services and the API."""

import json
from datetime import date

import pytest
from django.test import Client

from audit.actors import Actor
from core.errors import ValidationFailedError
from crm.contacts import services as contacts
from crm.pipelines import services
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def pipeline(workspace):
    return services.create_pipeline(
        workspace.pk, name="Sales", stages=[("New", 10), ("Won it", 80)]
    )


@pytest.fixture
def ada(workspace):
    return contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@engine.test"},
    )


@pytest.fixture
def grace(workspace):
    return contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"first_name": "Grace", "email": "g@navy.test"},
    )


@pytest.fixture
def deals(workspace, pipeline, ada, grace):
    def make(person, name, **kw):
        return services.create_opportunity(
            workspace.pk, SYSTEM, contact_id=person.pk, pipeline_id=pipeline.pk, name=name, **kw
        )

    return {
        "alpha": make(ada, "Alpha", value_micros=3_000_000, tags=["vip"], priority="high"),
        "beta": make(
            grace,
            "Beta",
            value_micros=1_000_000,
            source="Web",
            expected_close_date=date(2027, 3, 1),
        ),
        "gamma": make(grace, "Gamma", value_micros=2_000_000, tags=["vip", "renewal"]),
    }


def names(rows):
    return [d.name for d in rows]


def listed(workspace, **kw):
    return names(services.list_opportunities(workspace.pk, None, **kw))


def test_search_matches_deal_and_contact(workspace, deals):
    assert listed(workspace, q="alp") == ["Alpha"]
    assert listed(workspace, q="lovelace") == ["Alpha"]
    assert sorted(listed(workspace, q="navy.test")) == ["Beta", "Gamma"]
    assert len(listed(workspace, q="  ")) == 3


def test_filters_combine(workspace, deals):
    assert sorted(listed(workspace, tag="vip")) == ["Alpha", "Gamma"]
    assert listed(workspace, tag="vip", min_value_micros=2_500_000) == ["Alpha"]
    assert listed(workspace, priority="high") == ["Alpha"]
    assert listed(workspace, source="web") == ["Beta"]
    assert listed(workspace, close_after=date(2027, 1, 1), close_before=date(2027, 12, 31)) == [
        "Beta"
    ]
    assert len(listed(workspace, unassigned=True)) == 3


def test_sort_orders_and_refuses_unknown(workspace, deals):
    assert listed(workspace, sort="value") == ["Beta", "Gamma", "Alpha"]
    assert listed(workspace, sort="-value") == ["Alpha", "Gamma", "Beta"]
    assert listed(workspace, sort="name") == ["Alpha", "Beta", "Gamma"]
    with pytest.raises(ValidationFailedError):
        listed(workspace, sort="mood")
    with pytest.raises(ValidationFailedError):
        listed(workspace, favourite_colour="blue")


def test_summary_weights_by_stage_and_counts_outcomes(workspace, pipeline, deals):
    summary = services.deal_summary(workspace.pk, None, status="open")
    assert summary.count == 3 and summary.value_micros == 6_000_000
    assert summary.weighted_micros == 600_000 and summary.average_micros == 2_000_000
    services.win(workspace.pk, SYSTEM, deals["alpha"])
    services.lose(workspace.pk, SYSTEM, deals["beta"])
    after = services.deal_summary(workspace.pk, None, status="open")
    assert (after.count, after.won, after.lost) == (1, 1, 1)


def test_tags_in_use_are_sorted_and_distinct(workspace, deals):
    assert services.tags_in_use(workspace.pk) == ["renewal", "vip"]


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def test_api_filters_summary_tags_and_other_tenants_see_nothing(workspace, owner, pipeline, deals):
    me = Client()
    me.force_login(owner)
    found = api(me, "get", "/opportunities?q=alpha&sort=-value&limit=5", workspace).json()
    assert [d["name"] for d in found] == ["Alpha"] and found[0]["contact_name"] == "Ada Lovelace"
    assert len(api(me, "get", "/opportunities?offset=2&sort=name", workspace).json()) == 1
    assert api(me, "get", "/opportunities?sort=mood", workspace).status_code == 422
    summary = api(me, "get", "/opportunities/summary?status=open", workspace).json()
    assert summary["count"] == 3 and summary["value_micros"] == 6_000_000
    assert api(me, "get", "/opportunities/tags", workspace).json() == ["renewal", "vip"]
    other = create_workspace(name="Other Co", owner=owner)
    assert api(me, "get", "/opportunities", other).json() == []
    assert api(me, "get", "/opportunities/summary", other).json()["count"] == 0
    assert api(me, "get", "/opportunities/tags", other).json() == []
