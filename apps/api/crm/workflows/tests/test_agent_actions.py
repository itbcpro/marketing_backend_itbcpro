import pytest

from actions import services as actions
from actions.models import ActionStatus
from audit.actors import Actor
from conftest import make_user
from crm.contacts import services as contacts
from crm.workflows import agent_actions, definitions, engine
from crm.workflows.models import Enrollment
from workspaces.models import Role

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


@pytest.fixture
def flow(workspace):
    workflow = definitions.create_workflow(workspace.pk, name="Nurture")
    definitions.save_draft(
        workspace.pk,
        workflow.pk,
        {
            "trigger": {"type": "contact_created", "config": {}},
            "start": "w",
            "steps": [{"id": "w", "type": "wait", "config": {"seconds": 3600}}],
        },
    )
    return definitions.publish(workspace.pk, workflow.pk)


@pytest.fixture
def person(workspace):
    return contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "a@x.test"}
    )


def propose(workspace, flow, person):
    return actions.propose(
        workspace_id=workspace.pk,
        kind=agent_actions.ENROLL,
        payload={"workflow_id": str(flow.pk), "contact_id": str(person.pk)},
        requested_by="agent:sales",
        summary="Enrol Ada in the nurture flow",
    )


def test_an_agent_can_only_propose_and_nothing_happens_before_approval(workspace, flow, person):
    action = propose(workspace, flow, person)
    assert action.status == ActionStatus.AWAITING_APPROVAL and action.risk == "r2"
    assert Enrollment.objects.count() == 0


def test_approval_enrols_once_and_the_rollback_takes_them_out(workspace, flow, person):
    action = propose(workspace, flow, person)
    approver = make_user("approver@example.com", workspace, Role.APPROVER)
    actions.approve(workspace_id=workspace.pk, action_id=action.pk, user=approver)
    action.refresh_from_db()
    assert action.status in {ActionStatus.SUCCEEDED, ActionStatus.VERIFIED}
    assert action.result["enrolled"] is True
    enrollment = Enrollment.objects.get()
    assert enrollment.contact_id == person.pk
    engine.run(workspace.pk, enrollment.pk)

    undo = actions.request_rollback(workspace_id=workspace.pk, action_id=action.pk, user=approver)
    assert undo.kind == agent_actions.EXIT and undo.risk == "r1"
    actions.approve(workspace_id=workspace.pk, action_id=undo.pk, user=approver)
    enrollment.refresh_from_db()
    assert (enrollment.status, enrollment.exit_reason) == ("exited", "agent_rollback")


def test_a_viewer_cannot_approve_it(workspace, flow, person):
    from core.errors import PermissionDeniedError

    action = propose(workspace, flow, person)
    viewer = make_user("v@example.com", workspace, Role.VIEWER)
    with pytest.raises(PermissionDeniedError):
        actions.approve(workspace_id=workspace.pk, action_id=action.pk, user=viewer)
    assert Enrollment.objects.count() == 0


def test_someone_from_another_workspace_cannot_be_enrolled(workspace, owner, flow, person):
    from workspaces.services import create_workspace

    other = create_workspace(name="Other Co", owner=owner)
    action = actions.propose(
        workspace_id=other.pk,
        kind=agent_actions.ENROLL,
        payload={"workflow_id": str(flow.pk), "contact_id": str(person.pk)},
        requested_by="agent:sales",
    )
    actions.approve(workspace_id=other.pk, action_id=action.pk, user=owner)
    action.refresh_from_db()
    assert Enrollment.objects.count() == 0
    assert action.status != ActionStatus.SUCCEEDED or action.result["enrolled"] is False
