import json
from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone

from audit.actors import Actor
from audit.outbox import dispatch_batch
from conftest import make_user
from crm.activity import services as activity
from crm.contacts import services as contacts
from crm.notifications import services, tasks
from crm.notifications.models import Notification, NotificationKind
from workspaces.models import Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def rep(workspace):
    return make_user("rep@example.com", workspace, Role.EDITOR)


@pytest.fixture
def person(workspace):
    return contacts.create_contact(
        workspace_id=workspace.pk, actor=Actor.system("test"), data={"first_name": "Ada"}
    )


def run_outbox():
    dispatch_batch()


def kinds(user):
    return list(Notification.objects.filter(user=user).values_list("kind", flat=True))


def test_assigning_a_task_notifies_the_assignee_but_not_the_actor(workspace, owner, rep, person):
    activity.create_task(
        workspace_id=workspace.pk,
        actor=Actor.human(owner.pk),
        contact_id=person.pk,
        title="Call Ada",
        assigned_to=rep.pk,
    )
    activity.create_task(
        workspace_id=workspace.pk,
        actor=Actor.human(rep.pk),
        contact_id=person.pk,
        title="Own task",
        assigned_to=rep.pk,
    )
    run_outbox()
    assert kinds(rep) == [NotificationKind.TASK_ASSIGNED]


def test_mentions_notify_the_mentioned_people(workspace, owner, rep, person):
    activity.add_note(
        workspace_id=workspace.pk,
        actor=Actor.human(owner.pk),
        contact_id=person.pk,
        body="Please follow up",
        mentions=[rep.pk],
    )
    run_outbox()
    assert kinds(rep) == [NotificationKind.MENTION]
    assert kinds(owner) == []


def test_running_the_same_events_twice_notifies_once(workspace, owner, rep, person):
    activity.create_task(
        workspace_id=workspace.pk,
        actor=Actor.human(owner.pk),
        contact_id=person.pk,
        title="Once",
        assigned_to=rep.pk,
    )
    run_outbox()
    run_outbox()
    assert len(kinds(rep)) == 1


def test_preferences_switch_things_off_and_mail_only_is_born_read(workspace, owner, rep):
    services.set_preference(
        workspace.pk, rep.pk, NotificationKind.MENTION, in_app=False, email_digest=False
    )
    assert (
        services.notify(
            workspace.pk, recipient=rep.pk, actor_id=str(owner.pk), kind="mention", data={}
        )
        is None
    )
    services.set_preference(
        workspace.pk, rep.pk, NotificationKind.MENTION, in_app=False, email_digest=True
    )
    row = services.notify(
        workspace.pk, recipient=rep.pk, actor_id=str(owner.pk), kind="mention", data={}
    )
    assert row is not None and row.read_at is not None
    assert services.unread_count(workspace.pk, rep.pk) == 0


def test_defaults_and_invalid_kinds(workspace, rep):
    assert all(s.in_app and not s.email_digest for s in services.preferences(workspace.pk, rep.pk))
    from core.errors import ValidationFailedError

    with pytest.raises(ValidationFailedError):
        services.set_preference(workspace.pk, rep.pk, "nope", in_app=True, email_digest=False)


def test_read_state_is_personal(workspace, owner, rep):
    mine = services.notify(
        workspace.pk, recipient=rep.pk, actor_id=str(owner.pk), kind="mention", data={}
    )
    assert services.unread_count(workspace.pk, rep.pk) == 1
    from core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        services.mark_read(workspace.pk, owner.pk, mine.pk)  # someone else's
    services.mark_read(workspace.pk, rep.pk, mine.pk)
    services.mark_read(workspace.pk, rep.pk, mine.pk)  # repeating is harmless
    assert services.unread_count(workspace.pk, rep.pk) == 0


def test_digest_mails_old_unread_items_once(workspace, owner, rep, settings):
    settings.CRM_DIGEST_MIN_AGE_MINUTES = 30
    services.set_preference(
        workspace.pk, rep.pk, NotificationKind.MENTION, in_app=True, email_digest=True
    )
    old = services.notify(
        workspace.pk, recipient=rep.pk, actor_id=str(owner.pk), kind="mention", data={}
    )
    services.notify(  # too fresh to batch yet
        workspace.pk, recipient=rep.pk, actor_id=str(owner.pk), kind="mention", data={}
    )
    Notification.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=2))
    assert tasks.send_digests.apply().get() == 1
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["rep@example.com"]
    assert "You were mentioned in a note: 1" in mail.outbox[0].body
    assert tasks.send_digests.apply().get() == 0  # nothing is mailed twice


def test_digest_skips_read_items_and_people_who_did_not_ask(workspace, owner, rep):
    row = services.notify(
        workspace.pk, recipient=rep.pk, actor_id=str(owner.pk), kind="mention", data={}
    )
    Notification.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=1))
    assert tasks.send_digests.apply().get() == 0  # email is off by default
    services.set_preference(
        workspace.pk, rep.pk, NotificationKind.MENTION, in_app=True, email_digest=True
    )
    services.mark_read(workspace.pk, rep.pk, row.pk)
    assert tasks.send_digests.apply().get() == 0


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


def test_api_roundtrip(workspace, owner, rep):
    row = services.notify(
        workspace.pk, recipient=rep.pk, actor_id=str(owner.pk), kind="task_assigned", data={"a": 1}
    )
    client = logged(rep)
    assert api(client, "get", "/notifications/unread", workspace).json() == {"unread": 1}
    listed = api(client, "get", "/notifications?unread_only=true", workspace).json()
    assert listed[0]["kind"] == "task_assigned" and listed[0]["data"] == {"a": "1"}
    assert api(client, "post", f"/notifications/{row.pk}/read", workspace).status_code == 204
    assert api(client, "get", "/notifications/unread", workspace).json() == {"unread": 0}
    assert api(client, "post", "/notifications/read-all", workspace).json() == {"marked": 0}
    prefs = api(client, "get", "/notification-preferences", workspace).json()
    assert {p["kind"] for p in prefs} == set(NotificationKind.values)
    put = api(
        client,
        "put",
        "/notification-preferences/mention",
        workspace,
        {"in_app": False, "email_digest": True},
    )
    assert put.json() == {"kind": "mention", "in_app": False, "email_digest": True}
    bad = api(
        client,
        "put",
        "/notification-preferences/nope",
        workspace,
        {"in_app": True, "email_digest": True},
    )
    assert bad.status_code == 422


def test_api_is_personal_and_tenant_scoped(workspace, owner, rep):
    row = services.notify(
        workspace.pk, recipient=rep.pk, actor_id=str(owner.pk), kind="mention", data={}
    )
    assert api(logged(owner), "get", "/notifications", workspace).json() == []
    assert api(logged(owner), "post", f"/notifications/{row.pk}/read", workspace).status_code == 404
    other = create_workspace(name="Other Co", owner=rep)
    assert api(logged(rep), "get", "/notifications", other).json() == []
    assert api(logged(rep), "post", f"/notifications/{row.pk}/read", other).status_code == 404
