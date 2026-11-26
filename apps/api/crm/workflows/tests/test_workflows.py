import json
import uuid
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.outbox import dispatch_batch
from conftest import make_user
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from core.models import DeadLetter
from crm.channels.fake import FakeAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.inbox import services as inbox
from crm.inbox.models import Message
from crm.sending import registry
from crm.workflows import actions, definitions, engine
from crm.workflows.models import Enrollment, StepLog
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")


def definition(*steps, trigger=None, start=None):
    return {
        "trigger": trigger or {"type": "contact_created", "config": {}},
        "start": start or steps[0]["id"],
        "steps": list(steps),
    }


def step(sid, kind, config=None, to=None):
    out = {"id": sid, "type": kind, "config": config or {}}
    if to:
        out["next"] = to
    return out


@pytest.fixture
def person(workspace):
    c = contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"first_name": "Ada", "email": "ada@example.com"},
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=c.pk,
        channel="email",
        purpose="marketing",
        status="opted_in",
        source="form",
    )
    return c


@pytest.fixture
def fake_email():
    adapter = FakeAdapter("email")
    registry.register("email", lambda: adapter)
    return adapter


def make(workspace, *steps, name="Flow", reenter=False, stop_on_reply=True, **kw):
    workflow = definitions.create_workflow(
        workspace.pk, name=name, reenter=reenter, stop_on_reply=stop_on_reply
    )
    definitions.save_draft(workspace.pk, workflow.pk, definition(*steps, **kw))
    return definitions.publish(workspace.pk, workflow.pk)


def run_now(enrollment):
    return engine.run(enrollment.workspace_id, enrollment.pk)


def travel(enrollment, **delta):
    """Move a wait into the past, as if the time had come."""
    Enrollment.objects.filter(pk=enrollment.pk).update(
        wake_at=timezone.now() - timedelta(**delta or {"seconds": 1})
    )


# ---- definitions -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "kwargs", "code"),
    [
        ([], {}, "steps"),
        ([step("a", "mystery")], {}, "steps.a.type"),
        ([step("a", "add_tag")], {}, "steps.a.config.tag"),
        ([step("a", "finish", to="zzz")], {}, "steps.a"),
        ([step("a", "finish"), step("a", "finish")], {}, "steps"),
        ([step("a", "finish")], {"start": "nope"}, "start"),
        (
            [
                step("a", "add_tag", {"tag": "x"}, to="b"),
                step("b", "add_tag", {"tag": "y"}, to="a"),
            ],
            {},
            "steps",
        ),
        ([step("a", "finish")], {"trigger": {"type": "nope"}}, "trigger.type"),
        (
            [step("a", "finish")],
            {"trigger": {"type": "tag_added", "config": {}}},
            "trigger.config.tag",
        ),
    ],
)
def test_invalid_definitions_say_where(workspace, steps, kwargs, code):
    workflow = definitions.create_workflow(workspace.pk, name="X")
    body = (
        definition(*steps, **kwargs)
        if steps
        else {"trigger": {"type": "contact_created"}, "steps": []}
    )
    with pytest.raises(ValidationFailedError) as caught:
        definitions.save_draft(workspace.pk, workflow.pk, body)
    assert code in caught.value.params["errors"]


def test_a_published_version_never_changes_and_edits_make_a_successor(workspace):
    workflow = make(workspace, step("a", "add_tag", {"tag": "one"}))
    first = workflow.published_version
    definitions.save_draft(
        workspace.pk, workflow.pk, definition(step("a", "add_tag", {"tag": "two"}))
    )
    first.refresh_from_db()
    assert first.definition["steps"][0]["config"]["tag"] == "one"
    assert workflow.versions.count() == 2
    again = definitions.publish(workspace.pk, workflow.pk)
    assert again.published_version.number == 2
    with pytest.raises(ConflictError):
        definitions.publish(workspace.pk, workflow.pk)  # nothing new to publish


def test_names_are_unique_and_archiving_frees_them(workspace):
    first = definitions.create_workflow(workspace.pk, name="Welcome")
    with pytest.raises(ConflictError):
        definitions.create_workflow(workspace.pk, name="Welcome")
    definitions.archive(workspace.pk, first.pk)
    assert definitions.create_workflow(workspace.pk, name="Welcome").pk != first.pk
    with pytest.raises(NotFoundError):
        definitions.get_workflow(workspace.pk, first.pk)


# ---- running -----------------------------------------------------------------------------------


def test_steps_run_in_order_and_are_logged(workspace, person):
    workflow = make(
        workspace,
        step("tag", "add_tag", {"tag": "welcomed"}, to="task"),
        step("task", "create_task", {"title": "Call Ada", "due_in_days": 2}, to="end"),
        step("end", "finish"),
    )
    enrollment = engine.enroll(workspace.pk, workflow.pk, person.pk)
    assert run_now(enrollment) == "completed"
    enrollment.refresh_from_db()
    assert enrollment.status == "completed" and enrollment.finished_at
    assert [(log.step_id, log.outcome) for log in enrollment.log.all()] == [
        ("tag", "done"),
        ("task", "done"),
        ("end", "done"),
    ]
    assert contacts.tag_names_of([person.pk])[person.pk] == ["welcomed"]


def test_a_contact_enrolled_twice_with_reentry_off_runs_once(workspace, person):
    workflow = make(workspace, step("tag", "add_tag", {"tag": "x"}))
    first = engine.enroll(workspace.pk, workflow.pk, person.pk)
    assert first is not None
    assert engine.enroll(workspace.pk, workflow.pk, person.pk) is None  # still inside
    run_now(first)
    assert engine.enroll(workspace.pk, workflow.pk, person.pk) is None  # re-entry is off
    assert Enrollment.objects.filter(contact=person).count() == 1


def test_reentry_on_lets_a_finished_contact_back_in_but_never_twice_at_once(workspace, person):
    workflow = make(workspace, step("tag", "add_tag", {"tag": "x"}), reenter=True)
    first = engine.enroll(workspace.pk, workflow.pk, person.pk)
    assert engine.enroll(workspace.pk, workflow.pk, person.pk) is None
    run_now(first)
    second = engine.enroll(workspace.pk, workflow.pk, person.pk)
    assert second is not None and second.pk != first.pk


def test_paused_and_draft_workflows_let_nobody_in(workspace, person):
    workflow = make(workspace, step("tag", "add_tag", {"tag": "x"}))
    definitions.set_paused(workspace.pk, workflow.pk, True)
    assert engine.enroll(workspace.pk, workflow.pk, person.pk) is None
    draft = definitions.create_workflow(workspace.pk, name="Draft only")
    assert engine.enroll(workspace.pk, draft.pk, person.pk) is None


def test_a_wait_survives_a_restart_and_resumes_when_due(workspace, person, dispatched):
    workflow = make(
        workspace,
        step("pause", "wait", {"seconds": 3600}, to="tag"),
        step("tag", "add_tag", {"tag": "later"}),
    )
    enrollment = engine.enroll(workspace.pk, workflow.pk, person.pk)
    assert run_now(enrollment) == "waiting"
    enrollment.refresh_from_db()
    assert enrollment.status == "waiting" and enrollment.wake_at > timezone.now()
    assert run_now(enrollment) == "waiting"  # too early: nothing happens
    assert contacts.tag_names_of([person.pk]).get(person.pk, []) == []
    # A new process starts: all it has is the database.
    dispatched.clear()
    assert engine.wake_due() == 0
    travel(enrollment, seconds=5)
    assert engine.wake_due() == 1 and dispatched[-1] == (workspace.pk, enrollment.pk)
    assert run_now(enrollment) == "completed"
    assert contacts.tag_names_of([person.pk])[person.pk] == ["later"]


def test_a_failing_step_retries_with_backoff_then_lands_in_the_dead_letters(
    workspace, person, monkeypatch, settings
):
    settings.CRM_WORKFLOW_MAX_ATTEMPTS = 3
    settings.CRM_WORKFLOW_RETRY_BASE_SECONDS = 10
    calls = []

    def explode(self, ctx):
        calls.append(1)
        raise RuntimeError("boom with a@secret.test inside")

    monkeypatch.setattr(actions.AddTag, "execute", explode)
    workflow = make(
        workspace, step("tag", "add_tag", {"tag": "x"}, to="after"), step("after", "finish")
    )
    enrollment = engine.enroll(workspace.pk, workflow.pk, person.pk)
    waits = []
    for _ in range(3):
        run_now(enrollment)
        enrollment.refresh_from_db()
        if enrollment.status == "waiting":
            waits.append(round((enrollment.wake_at - timezone.now()).total_seconds() / 10))
            travel(enrollment)
    assert waits == [1, 2]  # 10 s, then 20 s
    assert enrollment.status == "failed" and enrollment.exit_reason == "step_failed"
    assert len(calls) == 3
    dead = DeadLetter.objects.get(task_name="crm.workflows.step")
    assert dead.kwargs["step_id"] == "tag"
    logs = list(enrollment.log.values_list("outcome", flat=True))
    assert logs == ["retry", "retry", "failed"]
    assert "secret.test" not in "".join(StepLog.objects.values_list("detail", flat=True))


def test_a_recovered_step_continues_and_resets_its_tries(workspace, person, monkeypatch):
    real = actions.AddTag.execute
    state = {"first": True}

    def flaky(self, ctx):
        if state.pop("first", False):
            raise RuntimeError("once")
        return real(self, ctx)

    monkeypatch.setattr(actions.AddTag, "execute", flaky)
    workflow = make(workspace, step("tag", "add_tag", {"tag": "x"}))
    enrollment = engine.enroll(workspace.pk, workflow.pk, person.pk)
    run_now(enrollment)
    travel(enrollment)
    assert run_now(enrollment) == "completed"
    assert contacts.tag_names_of([person.pk])[person.pk] == ["x"]


# ---- sending -----------------------------------------------------------------------------------


def test_messages_go_through_the_gateway_and_blocks_are_logged(workspace, person, fake_email):
    workflow = make(
        workspace,
        step(
            "mail",
            "send_message",
            {
                "channel": "email",
                "subject": "Hi {{contact.first_name}}",
                "body": "Hello {{contact.first_name}}",
                "purpose": "marketing",
            },
            to="sms",
        ),
        step("sms", "send_message", {"channel": "sms", "body": "no consent"}, to="end"),
        step("end", "finish"),
    )
    enrollment = engine.enroll(workspace.pk, workflow.pk, person.pk)
    run_now(enrollment)
    assert fake_email.sent[0].body == "Hello Ada" and fake_email.sent[0].subject == "Hi Ada"
    sms = StepLog.objects.get(step_id="sms")
    assert sms.outcome == "skipped" and sms.detail
    enrollment.refresh_from_db()
    assert enrollment.status == "completed"


def test_a_send_blocked_to_stop_ends_the_enrollment(workspace, person):
    workflow = make(
        workspace,
        step(
            "sms", "send_message", {"channel": "sms", "body": "x", "on_blocked": "stop"}, to="tag"
        ),
        step("tag", "add_tag", {"tag": "never"}),
    )
    enrollment = engine.enroll(workspace.pk, workflow.pk, person.pk)
    run_now(enrollment)
    enrollment.refresh_from_db()
    assert enrollment.status == "exited" and enrollment.exit_reason
    assert contacts.tag_names_of([person.pk]).get(person.pk, []) == []


def test_a_retried_send_never_messages_twice(workspace, person, fake_email, monkeypatch):
    workflow = make(
        workspace,
        step("mail", "send_message", {"channel": "email", "body": "Hi", "subject": "S"}, to="end"),
        step("end", "finish"),
    )
    enrollment = engine.enroll(workspace.pk, workflow.pk, person.pk)
    # The worker dies after the send but before the state is saved: simulate by replaying it.
    run_now(enrollment)
    Enrollment.objects.filter(pk=enrollment.pk).update(status="active", current_step="mail")
    run_now(enrollment)
    assert Message.objects.filter(channel="email", direction="outbound").count() == 1


# ---- branches, events and replies ----------------------------------------------------------------


def test_branches_follow_the_contact_data(workspace, person):
    workflow = make(
        workspace,
        step(
            "check",
            "branch",
            {
                "rules": [{"field": "city", "op": "equals", "value": "Toronto"}],
                "yes": "local",
                "no": "far",
            },
        ),
        step("local", "add_tag", {"tag": "local"}),
        step("far", "add_tag", {"tag": "far"}),
    )
    contacts.update_contact(
        workspace_id=workspace.pk, actor=SYSTEM, contact_id=person.pk, data={"city": "toronto"}
    )
    run_now(engine.enroll(workspace.pk, workflow.pk, person.pk))
    assert contacts.tag_names_of([person.pk])[person.pk] == ["local"]


def test_waiting_for_a_reply_continues_on_reply_and_on_timeout(workspace, person):
    flow = [
        step(
            "ask",
            "wait_for_event",
            {"event": "reply", "timeout_seconds": 600, "on_event": "yes", "on_timeout": "no"},
        ),
        step("yes", "add_tag", {"tag": "replied"}),
        step("no", "add_tag", {"tag": "silent"}),
    ]
    workflow = make(workspace, *flow, stop_on_reply=True)
    one = engine.enroll(workspace.pk, workflow.pk, person.pk)
    assert run_now(one) == "waiting"
    engine.on_inbound_message(workspace.pk, person.pk)
    assert run_now(one) == "completed"
    assert contacts.tag_names_of([person.pk])[person.pk] == ["replied"]
    other = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"email": "b@example.com"}
    )
    two = engine.enroll(workspace.pk, workflow.pk, other.pk)
    run_now(two)
    travel(two, seconds=1)
    assert run_now(two) == "completed"
    assert contacts.tag_names_of([other.pk])[other.pk] == ["silent"]


def test_a_reply_stops_flows_that_say_so_and_leaves_the_others(workspace, person):
    stopping = make(workspace, step("w", "wait", {"seconds": 99}), name="Stops", stop_on_reply=True)
    staying = make(workspace, step("w", "wait", {"seconds": 99}), name="Stays", stop_on_reply=False)
    a = engine.enroll(workspace.pk, stopping.pk, person.pk)
    b = engine.enroll(workspace.pk, staying.pk, person.pk)
    run_now(a)
    run_now(b)
    engine.on_inbound_message(workspace.pk, person.pk)
    a.refresh_from_db()
    b.refresh_from_db()
    assert (a.status, a.exit_reason) == ("exited", "replied") and b.status == "waiting"


def test_triggers_enrol_through_events(workspace):
    make(
        workspace,
        step("tag", "add_tag", {"tag": "new"}),
        trigger={"type": "contact_created", "config": {"source": "web"}},
    )
    make(
        workspace,
        step("tag", "add_tag", {"tag": "vip-flow"}),
        name="Tagged",
        trigger={"type": "tag_added", "config": {"tag": "VIP"}},
    )
    web = contacts.create_contact(workspace_id=workspace.pk, actor=SYSTEM, data={"source": "web"})
    other = contacts.create_contact(
        workspace_id=workspace.pk, actor=SYSTEM, data={"source": "import"}
    )
    contacts.add_tags(workspace_id=workspace.pk, actor=SYSTEM, contact=other, names=["vip"])
    dispatch_batch()
    dispatch_batch()  # the same events again: nobody is enrolled twice
    rows = {(e.workflow.name, e.contact_id) for e in Enrollment.objects.select_related("workflow")}
    assert rows == {("Flow", web.pk), ("Tagged", other.pk)}


def test_inbound_messages_reach_the_engine_through_the_outbox(workspace, person):
    from datetime import UTC, datetime

    workflow = make(workspace, step("w", "wait", {"seconds": 99}))
    enrollment = engine.enroll(workspace.pk, workflow.pk, person.pk)
    run_now(enrollment)
    inbox.ingest(
        workspace_id=workspace.pk,
        item=inbox.InboundMessage(
            channel="email",
            provider_message_id="m1",
            sender_channel="email",
            sender_value="ada@example.com",
            provider_ts=datetime.now(UTC),
            body="Hello",
        ),
    )
    dispatch_batch()
    enrollment.refresh_from_db()
    assert enrollment.exit_reason == "replied"


# ---- API ---------------------------------------------------------------------------------------


def api(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


def logged(user):
    c = Client()
    c.force_login(user)
    return c


def test_api_roundtrip_with_the_builder_catalog(workspace, owner, person):
    admin = logged(owner)
    catalog = api(admin, "get", "/workflows/catalog", workspace).json()
    assert {"send_message", "wait", "branch"} <= {a["key"] for a in catalog["actions"]}
    assert "contact_created" in {t["key"] for t in catalog["triggers"]}
    made = api(admin, "post", "/workflows", workspace, {"name": "Flow"})
    assert made.status_code == 201
    wid = made.json()["id"]
    body = definition(step("tag", "add_tag", {"tag": "x"}))
    bad = api(
        admin, "put", f"/workflows/{wid}/definition", workspace, {"definition": {"steps": []}}
    )
    assert bad.status_code == 422
    assert (
        api(
            admin, "put", f"/workflows/{wid}/definition", workspace, {"definition": body}
        ).status_code
        == 200
    )
    published = api(admin, "post", f"/workflows/{wid}/publish", workspace).json()
    assert published["status"] == "active" and published["published_number"] == 1
    enrolled = api(
        admin, "post", f"/workflows/{wid}/enroll", workspace, {"contact_id": str(person.pk)}
    )
    assert enrolled.status_code == 201
    eid = enrolled.json()["id"]
    again = api(
        admin, "post", f"/workflows/{wid}/enroll", workspace, {"contact_id": str(person.pk)}
    )
    assert again.status_code == 200 and again.content in (b"", b"null")
    engine.run(workspace.pk, uuid.UUID(eid))
    log = api(admin, "get", f"/enrollments/{eid}/log", workspace).json()
    assert [entry["outcome"] for entry in log] == ["done"]
    assert len(api(admin, "get", f"/workflows/{wid}/enrollments", workspace).json()) == 1
    assert len(api(admin, "get", f"/contacts/{person.pk}/enrollments", workspace).json()) == 1
    paused = api(admin, "post", f"/workflows/{wid}/paused", workspace, {"paused": True}).json()
    assert paused["status"] == "paused"
    assert api(admin, "delete", f"/workflows/{wid}", workspace).status_code == 204


def test_permissions_and_tenants(workspace, owner, person):
    flow = make(workspace, step("w", "wait", {"seconds": 5}))
    enrollment = engine.enroll(workspace.pk, flow.pk, person.pk)
    viewer = logged(make_user("v@example.com", workspace, Role.VIEWER))
    assert api(viewer, "get", "/workflows", workspace).status_code == 403
    assert api(viewer, "post", "/workflows", workspace, {"name": "X"}).status_code == 403
    assert api(viewer, "get", f"/contacts/{person.pk}/enrollments", workspace).status_code == 200
    other = create_workspace(name="Other Co", owner=owner)
    admin = logged(owner)
    assert api(admin, "get", "/workflows", other).json() == []
    for method, path, payload in (
        ("get", f"/workflows/{flow.pk}/definition", None),
        ("put", f"/workflows/{flow.pk}/definition", {"definition": {}}),
        ("post", f"/workflows/{flow.pk}/publish", None),
        ("post", f"/workflows/{flow.pk}/paused", {"paused": True}),
        ("post", f"/workflows/{flow.pk}/enroll", {"contact_id": str(person.pk)}),
        ("get", f"/workflows/{flow.pk}/enrollments", None),
        ("get", f"/enrollments/{enrollment.pk}/log", None),
        ("post", f"/enrollments/{enrollment.pk}/exit", None),
        ("get", f"/contacts/{person.pk}/enrollments", None),
        ("delete", f"/workflows/{flow.pk}", None),
    ):
        assert api(admin, method, path, other, payload).status_code == 404, path
    assert api(admin, "post", f"/enrollments/{enrollment.pk}/exit", workspace).status_code == 204
    enrollment.refresh_from_db()
    assert enrollment.exit_reason == "removed"


# ---- test mode ---------------------------------------------------------------------------------


def test_simulation_walks_the_real_contact_without_side_effects(
    workspace, owner, person, fake_email
):
    contacts.update_contact(
        workspace_id=workspace.pk, actor=SYSTEM, contact_id=person.pk, data={"city": "Toronto"}
    )
    body = definition(
        step("mail", "send_message", {"channel": "email", "body": "Hi"}, to="pause"),
        step("pause", "wait", {"seconds": 86400}, to="check"),
        step(
            "check",
            "branch",
            {
                "rules": [{"field": "city", "op": "equals", "value": "Toronto"}],
                "yes": "end",
                "no": "other",
            },
        ),
        step("other", "add_tag", {"tag": "far"}, to="end"),
        step("end", "finish"),
    )
    workflow = definitions.create_workflow(workspace.pk, name="Test")
    admin = logged(owner)
    out = api(
        admin,
        "post",
        f"/workflows/{workflow.pk}/simulate",
        workspace,
        {"contact_id": str(person.pk), "definition": body},
    )
    assert out.status_code == 200
    assert [(v["step_id"], v["note"]) for v in out.json()] == [
        ("mail", "send"),
        ("pause", "wait"),
        ("check", "yes"),
        ("end", "finish"),
    ]
    assert fake_email.sent == [] and Message.objects.count() == 0
    assert contacts.tag_names_of([person.pk]).get(person.pk, []) == []
    bad = api(
        admin,
        "post",
        f"/workflows/{workflow.pk}/simulate",
        workspace,
        {"contact_id": str(person.pk), "definition": {"steps": []}},
    )
    assert bad.status_code == 422
    other = create_workspace(name="Other Co", owner=owner)
    nope = api(
        admin, "post", f"/workflows/{workflow.pk}/simulate", other, {"contact_id": str(person.pk)}
    )
    assert nope.status_code == 404
