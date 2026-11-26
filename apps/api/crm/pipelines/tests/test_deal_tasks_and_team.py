"""Tasks on deals, the task summary on deal cards, and the team picker endpoint."""

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from conftest import make_user
from core.errors import NotFoundError, ValidationFailedError
from crm.activity import services as activity
from crm.contacts import services as contacts
from crm.pipelines import services
from workspaces import services as workspace_services
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def pipeline(workspace):
    return services.create_pipeline(workspace.pk, name="Sales", stages=[("New", 10)])


def person(workspace, first, email):
    return contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"first_name": first, "email": email}
    )


def deal_of(workspace, pipeline, contact, name="Deal"):
    return services.create_opportunity(
        workspace.pk, SYSTEM, contact_id=contact.pk, pipeline_id=pipeline.pk, name=name
    )


def task(workspace, contact, deal, **kw):
    return activity.create_task(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=contact.pk,
        title=kw.pop("title", "Call back"),
        opportunity_id=deal.pk if deal else None,
        **kw,
    )


def test_task_links_to_the_deal_of_its_contact(workspace, pipeline):
    ada, grace = person(workspace, "Ada", "a@x.test"), person(workspace, "Grace", "g@x.test")
    deal = deal_of(workspace, pipeline, ada)
    assert task(workspace, ada, deal).opportunity_id == deal.pk
    with pytest.raises(ValidationFailedError):
        task(workspace, grace, deal)
    assert task(workspace, ada, None).opportunity_id is None


def test_task_for_a_deal_of_another_workspace_is_refused(workspace, owner, pipeline):
    ada = person(workspace, "Ada", "a@x.test")
    deal = deal_of(workspace, pipeline, ada)
    other = create_workspace(name="Other Co", owner=owner)
    stranger = contacts.create_contact(
        workspace_id=other.pk, actor=SYSTEM, data={"first_name": "Z", "email": "z@x.test"}
    )
    with pytest.raises(NotFoundError):
        activity.create_task(
            workspace_id=other.pk,
            actor=SYSTEM,
            contact_id=stranger.pk,
            title="x",
            opportunity_id=deal.pk,
        )


def test_summary_counts_open_overdue_and_next_due(workspace, pipeline):
    ada = person(workspace, "Ada", "a@x.test")
    deal, quiet = deal_of(workspace, pipeline, ada), deal_of(workspace, pipeline, ada, "Quiet")
    now = timezone.now()
    task(workspace, ada, deal, due_at=now - timedelta(days=1))
    soon = now + timedelta(days=2)
    task(workspace, ada, deal, due_at=soon)
    done = task(workspace, ada, deal, due_at=now + timedelta(days=9))
    activity.complete_task(workspace_id=workspace.pk, actor=SYSTEM, task_id=done.pk)
    found = activity.open_task_summary(workspace.pk, [deal.pk, quiet.pk])
    assert quiet.pk not in found
    assert (found[deal.pk]["open"], found[deal.pk]["overdue"]) == (2, 1)
    assert found[deal.pk]["next_due_at"] < soon


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def test_api_shows_tasks_on_deals_and_filters_tasks_by_deal(workspace, owner, pipeline):
    me = Client()
    me.force_login(owner)
    ada = person(workspace, "Ada", "a@x.test")
    deal, other = deal_of(workspace, pipeline, ada), deal_of(workspace, pipeline, ada, "Other")
    made = api(
        me,
        "post",
        f"/contacts/{ada.pk}/tasks",
        workspace,
        {"title": "Send quote", "opportunity_id": str(deal.pk)},
    )
    assert made.status_code == 201 and made.json()["opportunity_id"] == str(deal.pk)
    card = api(me, "get", f"/opportunities/{deal.pk}", workspace).json()
    assert (card["open_tasks"], card["overdue_tasks"]) == (1, 0)
    assert api(me, "get", f"/opportunities/{other.pk}", workspace).json()["open_tasks"] == 0
    mine = api(me, "get", f"/tasks?scope=all&opportunity_id={deal.pk}", workspace).json()
    assert [t["title"] for t in mine["items"]] == ["Send quote"]
    assert (
        api(me, "get", f"/tasks?scope=all&opportunity_id={other.pk}", workspace).json()["items"]
        == []
    )


def test_team_lists_members_and_never_other_workspaces(workspace, owner):
    me = Client()
    me.force_login(owner)
    mate = make_user("mate@example.com")
    workspace_services.add_member(workspace=workspace, user=mate, role=Role.EDITOR, added_by=owner)
    team = api(me, "get", "/team", workspace).json()
    assert {m["email"] for m in team} == {owner.email, "mate@example.com"}
    assert {m["role"] for m in team} == {"owner", "editor"}
    other = create_workspace(name="Other Co", owner=owner)
    assert [m["email"] for m in api(me, "get", "/team", other).json()] == [owner.email]
    outsider = Client()
    outsider.force_login(make_user("out@example.com"))
    refused = api(outsider, "get", "/team", workspace)
    assert refused.status_code in (403, 404, 422) and "mate@example.com" not in str(refused.content)
