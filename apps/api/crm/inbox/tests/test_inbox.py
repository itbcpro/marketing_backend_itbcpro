import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from django.test import Client

from audit.actors import Actor
from audit.models import OutboxMessage
from audit.outbox import dispatch_batch
from conftest import make_user
from core.errors import ValidationFailedError
from crm.consent import services as consent
from crm.consent.keywords import KeywordAction
from crm.contacts import services as contacts
from crm.inbox import services
from crm.inbox.models import ChannelAccount, Conversation, Message
from workspaces.models import Membership, RecordScope, Role
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
T0 = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)


def inbound(workspace, pid="m1", body="hello", **extra):
    values = {
        "channel": "sms",
        "provider_message_id": pid,
        "sender_channel": "phone",
        "sender_value": "416-555-0199",
        "provider_ts": T0,
        "body": body,
        **extra,
    }
    return services.ingest(workspace_id=workspace.pk, item=services.InboundMessage(**values))


def login(user):
    client = Client()
    client.force_login(user)
    return client


def call(client, method, path, workspace, body=None):
    kwargs = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    if body is not None:
        kwargs |= {"data": json.dumps(body), "content_type": "application/json"}
    return getattr(client, method)(f"/api/v1/crm{path}", **kwargs)


# ---- ingestion ---------------------------------------------------------------------------------


def test_first_message_creates_the_contact_the_thread_and_an_event(workspace):
    result = inbound(workspace, sender_name="Ada Lovelace")
    assert result.created and result.contact.phone == "+14165550199"
    assert (result.contact.first_name, result.contact.last_name, result.contact.source) == (
        "Ada",
        "Lovelace",
        "sms",
    )
    conversation = result.message.conversation
    assert (conversation.last_preview, conversation.last_channel) == ("hello", "sms")
    assert conversation.last_inbound_at == T0
    assert OutboxMessage.objects.filter(topic="crm.message.inbound").count() == 1


def test_the_same_delivery_twice_stores_one_message(workspace):
    first = inbound(workspace)
    again = inbound(workspace)
    assert first.created and not again.created and first.message.pk == again.message.pk
    assert Message.objects.count() == 1
    assert OutboxMessage.objects.filter(topic="crm.message.inbound").count() == 1


def test_existing_contacts_are_matched_across_channels_into_one_thread(workspace):
    known = contacts.create_contact(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        data={"email": "ada@example.com", "phone": "416-555-0199"},
    )
    by_sms = inbound(workspace, "s1")
    by_mail = inbound(
        workspace,
        "e1",
        channel="email",
        sender_channel="email",
        sender_value=" ADA@example.com ",
        subject="Hi",
        body="mail body",
        provider_ts=T0 + timedelta(minutes=5),
    )
    assert by_sms.contact.pk == by_mail.contact.pk == known.pk
    assert Conversation.objects.count() == 1
    conversation = Conversation.objects.get()
    assert (conversation.last_channel, conversation.last_message_at) == (
        "email",
        T0 + timedelta(minutes=5),
    )
    assert (
        contacts.query_contacts(
            Membership.objects.get(workspace=workspace, user=workspace.memberships.first().user)
        ).count()
        == 1
    )


def test_social_senders_become_identities_of_a_new_contact(workspace):
    result = inbound(
        workspace,
        channel="instagram",
        sender_channel="instagram",
        sender_value="igsid-42",
        sender_name="Zed",
    )
    again = inbound(
        workspace, "m2", channel="instagram", sender_channel="instagram", sender_value="igsid-42"
    )
    assert result.contact.pk == again.contact.pk
    assert contacts.find_contact(workspace.pk, "instagram", "igsid-42").pk == result.contact.pk


@pytest.mark.parametrize(
    "bad",
    [
        {"sender_value": "nope", "sender_channel": "email"},
        {"sender_value": "12", "sender_channel": "phone"},
        {"sender_value": "  ", "sender_channel": "instagram"},
        {"provider_message_id": ""},
        {"channel": "pigeon"},
    ],
)
def test_invalid_deliveries_are_rejected_and_store_nothing(workspace, bad):
    with pytest.raises(ValidationFailedError):
        inbound(workspace, **bad)
    assert Message.objects.count() == 0 and not Conversation.objects.exists()


def test_stop_and_start_change_consent_through_the_pipeline(workspace):
    first = inbound(workspace, "a", body="hello")
    assert first.keyword is None
    stop = inbound(workspace, "b", body="STOP")
    assert stop.keyword == KeywordAction.OPT_OUT
    assert (
        consent.decide(
            workspace_id=workspace.pk,
            contact_id=stop.contact.pk,
            channel="sms",
            purpose="transactional",
        ).reason
        == "opted_out"
    )
    start = inbound(workspace, "c", body="start")
    assert start.keyword == KeywordAction.OPT_IN
    assert consent.decide(
        workspace_id=workspace.pk,
        contact_id=stop.contact.pk,
        channel="sms",
        purpose="transactional",
    ).allowed
    mail = inbound(
        workspace,
        "d",
        channel="email",
        sender_channel="email",
        sender_value="x@example.com",
        body="STOP",
    )
    assert mail.keyword is None  # email unsubscribes use a link, not a keyword


def test_a_new_inbound_message_reopens_a_closed_thread(workspace):
    result = inbound(workspace)
    cid = result.message.conversation_id
    services.set_flags(workspace_id=workspace.pk, conversation_id=cid, status="closed")
    inbound(workspace, "m2", provider_ts=T0 + timedelta(hours=1))
    assert Conversation.objects.get(pk=cid).status == "open"


# ---- outbound records and status callbacks -----------------------------------------------------


def outbound(workspace, contact, **extra):
    return services.create_outbound(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact=contact,
        channel="sms",
        body="reply",
        **extra,
    )


def test_status_never_moves_backwards_and_callbacks_may_arrive_out_of_order(workspace):
    contact = inbound(workspace).contact
    message = outbound(workspace, contact)
    services.mark_sent(
        workspace_id=workspace.pk, message_id=message.pk, channel="sms", provider_message_id="p1"
    )
    apply = lambda status, minutes=0: services.apply_status(  # noqa: E731
        workspace_id=workspace.pk,
        channel="sms",
        provider_message_id="p1",
        status=status,
        provider_ts=T0 + timedelta(minutes=minutes),
    )
    assert apply("delivered", 2).status == "delivered"
    assert apply("sent", 1).status == "delivered"  # late "sent" is ignored
    assert apply("delivered", 3).status == "delivered"  # repeats are harmless
    assert apply("read", 4).status == "read"
    assert apply("failed", 5).status == "read"  # a finished message cannot fail
    assert message.status_events.count() == 5
    # sent (from mark_sent), delivered and read each announce once; the ignored callbacks announce nothing
    assert OutboxMessage.objects.filter(topic="crm.message.status_changed").count() == 3


def test_failure_replaces_an_in_flight_message_only(workspace):
    contact = inbound(workspace).contact
    message = outbound(workspace, contact)
    services.mark_sent(
        workspace_id=workspace.pk, message_id=message.pk, channel="sms", provider_message_id="p2"
    )
    failed = services.apply_status(
        workspace_id=workspace.pk,
        channel="sms",
        provider_message_id="p2",
        status="failed",
        provider_ts=T0,
        error_code="30003",
    )
    assert (failed.status, failed.error_code) == ("failed", "30003")
    later = services.apply_status(
        workspace_id=workspace.pk,
        channel="sms",
        provider_message_id="p2",
        status="delivered",
        provider_ts=T0,
    )
    assert later.status == "failed"


def test_unknown_provider_ids_and_bad_statuses(workspace):
    assert (
        services.apply_status(
            workspace_id=workspace.pk,
            channel="sms",
            provider_message_id="nope",
            status="sent",
            provider_ts=T0,
        )
        is None
    )
    with pytest.raises(ValidationFailedError):
        services.apply_status(
            workspace_id=workspace.pk,
            channel="sms",
            provider_message_id="x",
            status="teleported",
            provider_ts=T0,
        )


def test_blocked_outbound_messages_are_recorded_with_their_reason(workspace):
    contact = inbound(workspace).contact
    blocked = outbound(workspace, contact, status="blocked", error_code="opted_out")
    assert (blocked.status, blocked.error_code) == ("blocked", "opted_out")
    assert Conversation.objects.get().last_preview == "reply"


# ---- comments, assignment, read state ----------------------------------------------------------


def test_internal_comments_stay_internal(workspace, owner):
    result = inbound(workspace)
    cid = result.message.conversation_id
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    comment = services.add_comment(
        workspace_id=workspace.pk,
        actor=Actor.human(owner.pk),
        conversation_id=cid,
        body=" call back ",
        mentions=[rep.pk],
    )
    assert (comment.direction, comment.body, comment.mentions) == (
        "internal",
        "call back",
        [str(rep.pk)],
    )
    conversation = Conversation.objects.get(pk=cid)
    assert conversation.last_preview == "hello"  # comments do not change the thread summary
    with pytest.raises(ValidationFailedError):
        services.add_comment(
            workspace_id=workspace.pk,
            actor=SYSTEM,
            conversation_id=cid,
            body="x",
            mentions=[uuid.uuid4()],
        )
    with pytest.raises(ValidationFailedError):
        services.add_comment(
            workspace_id=workspace.pk, actor=SYSTEM, conversation_id=cid, body="  "
        )


def test_assigning_a_thread_assigns_the_contact(workspace, owner):
    result = inbound(workspace)
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    services.assign(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        conversation_id=result.message.conversation_id,
        user_id=rep.pk,
    )
    result.contact.refresh_from_db()
    assert result.contact.assigned_to_id == rep.pk
    assert Conversation.objects.get().assigned_to_id == rep.pk
    assert OutboxMessage.objects.filter(topic="crm.conversation.assigned").count() == 1


def test_read_state_is_per_member(workspace, owner):
    result = inbound(workspace)
    cid = result.message.conversation_id
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    assert services.unread_ids(owner.pk, [cid]) == {cid} == services.unread_ids(rep.pk, [cid])
    services.mark_read(workspace_id=workspace.pk, conversation_id=cid, user_id=owner.pk)
    assert services.unread_ids(owner.pk, [cid]) == set() and services.unread_ids(rep.pk, [cid]) == {
        cid
    }
    inbound(workspace, "m2", provider_ts=datetime.now(UTC) + timedelta(minutes=1))
    assert services.unread_ids(owner.pk, [cid]) == {cid}  # a newer inbound makes it unread again
    services.mark_unread(workspace_id=workspace.pk, conversation_id=cid, user_id=owner.pk)
    assert services.unread_ids(owner.pk, [cid]) == {cid}


# ---- realtime ----------------------------------------------------------------------------------


def test_inbound_messages_are_pushed_to_the_workspace_without_their_text(workspace):
    result = inbound(workspace, body="secret words")
    with mock.patch("crm.inbox.consumers.sse.publish") as publish:
        dispatch_batch()
        dispatch_batch()  # redelivery must not push twice
    pushes = [c for c in publish.call_args_list if c.args[2]["topic"] == "crm.message.inbound"]
    assert len(pushes) == 1
    channel, event, data = pushes[0].args
    assert channel == f"ws:{workspace.pk}" and event == "inbox"
    assert data["conversation_id"] == str(result.message.conversation_id)
    assert "secret words" not in json.dumps(data)


# ---- API ---------------------------------------------------------------------------------------


def test_api_list_filters_threads_and_flags(workspace, owner):
    admin = login(owner)
    a = inbound(workspace, "a", body="pricing question", sender_value="416-555-0101")
    b = inbound(
        workspace,
        "b",
        channel="email",
        sender_channel="email",
        sender_value="b@example.com",
        body="invoice",
        provider_ts=T0 + timedelta(minutes=10),
    )
    listed = call(admin, "get", "/inbox/conversations", workspace).json()
    assert [i["last_preview"] for i in listed["items"]] == ["invoice", "pricing question"]
    assert (
        call(admin, "get", "/inbox/conversations?channel=email", workspace).json()["items"][0][
            "last_preview"
        ]
        == "invoice"
    )
    assert [
        i["last_preview"]
        for i in call(admin, "get", "/inbox/conversations?q=pricing", workspace).json()["items"]
    ] == ["pricing question"]
    assert (
        len(call(admin, "get", "/inbox/conversations?tab=unread", workspace).json()["items"]) == 2
    )
    first = call(admin, "get", "/inbox/conversations?limit=1", workspace).json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    rest = call(
        admin, "get", f"/inbox/conversations?limit=1&cursor={first['next_cursor']}", workspace
    ).json()
    assert rest["items"][0]["id"] != first["items"][0]["id"] and rest["next_cursor"] is None
    cid = str(a.message.conversation_id)
    assert call(admin, "post", f"/inbox/conversations/{cid}/read", workspace).status_code == 204
    assert (
        len(call(admin, "get", "/inbox/conversations?tab=unread", workspace).json()["items"]) == 1
    )
    starred = call(admin, "patch", f"/inbox/conversations/{cid}", workspace, {"starred": True})
    assert starred.json()["starred"] is True and starred.json()["unread"] is False
    assert [
        i["id"]
        for i in call(admin, "get", "/inbox/conversations?tab=starred", workspace).json()["items"]
    ] == [cid]
    assert call(admin, "get", "/inbox/conversations?tab=nope", workspace).status_code == 422
    assert (
        call(admin, "get", f"/contacts/{b.contact.pk}/conversation", workspace).json()[
            "last_preview"
        ]
        == "invoice"
    )


def test_api_thread_comments_assign(workspace, owner):
    admin = login(owner)
    rep = make_user("rep@example.com", workspace, Role.EDITOR)
    result = inbound(workspace, "a", provider_ts=T0)
    inbound(workspace, "b", body="second", provider_ts=T0 + timedelta(minutes=1))
    cid = str(result.message.conversation_id)
    thread = call(admin, "get", f"/inbox/conversations/{cid}/messages?limit=1", workspace).json()
    assert [m["body"] for m in thread["items"]] == ["second"] and thread["next_cursor"]
    older = call(
        admin,
        "get",
        f"/inbox/conversations/{cid}/messages?limit=5&cursor={thread['next_cursor']}",
        workspace,
    ).json()
    assert [m["body"] for m in older["items"]] == ["hello"]
    note = call(
        admin,
        "post",
        f"/inbox/conversations/{cid}/comments",
        workspace,
        {"body": "ping", "mentions": [str(rep.pk)]},
    )
    assert note.status_code == 201 and note.json()["direction"] == "internal"
    assigned = call(
        admin, "post", f"/inbox/conversations/{cid}/assign", workspace, {"user_id": str(rep.pk)}
    )
    assert assigned.json()["assigned_to"] == str(rep.pk)
    assert call(admin, "get", "/inbox/conversations?tab=mine", workspace).json()["items"] == []
    assert (
        len(call(login(rep), "get", "/inbox/conversations?tab=mine", workspace).json()["items"])
        == 1
    )
    assert (
        call(admin, "get", "/inbox/conversations?tab=unassigned", workspace).json()["items"] == []
    )


def test_api_permissions_scope_and_tenants(workspace, owner):
    viewer = login(make_user("v@example.com", workspace, Role.VIEWER))
    rep_user = make_user("rep@example.com", workspace, Role.EDITOR)
    Membership.objects.filter(user=rep_user).update(record_scope=RecordScope.ASSIGNED)
    rep = login(rep_user)
    mine = inbound(workspace, "a", sender_value="416-555-0101")
    theirs = inbound(workspace, "b", sender_value="416-555-0102")
    admin = login(owner)
    services.assign(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        conversation_id=mine.message.conversation_id,
        user_id=rep_user.pk,
    )
    mine_id, theirs_id = str(mine.message.conversation_id), str(theirs.message.conversation_id)
    assert (
        call(
            viewer, "post", f"/inbox/conversations/{mine_id}/comments", workspace, {"body": "x"}
        ).status_code
        == 403
    )
    assert [
        i["id"] for i in call(rep, "get", "/inbox/conversations", workspace).json()["items"]
    ] == [mine_id]
    for method, path, body in (
        ("get", f"/inbox/conversations/{theirs_id}", None),
        ("get", f"/inbox/conversations/{theirs_id}/messages", None),
        ("post", f"/inbox/conversations/{theirs_id}/comments", {"body": "x"}),
        ("post", f"/inbox/conversations/{theirs_id}/read", None),
        ("post", f"/inbox/conversations/{theirs_id}/assign", {"user_id": None}),
        ("patch", f"/inbox/conversations/{theirs_id}", {"starred": True}),
    ):
        assert call(rep, method, path, workspace, body).status_code == 404, path
    other = create_workspace(name="Other Co", owner=owner)
    assert call(admin, "get", "/inbox/conversations", other).json()["items"] == []
    for method, path, body in (
        ("get", f"/inbox/conversations/{theirs_id}", None),
        ("get", f"/inbox/conversations/{theirs_id}/messages", None),
        ("post", f"/inbox/conversations/{theirs_id}/comments", {"body": "x"}),
        ("post", f"/inbox/conversations/{theirs_id}/assign", {"user_id": None}),
        ("get", f"/contacts/{theirs.contact.pk}/conversation", None),
    ):
        assert call(admin, method, path, other, body).status_code == 404, path


def test_same_provider_id_in_two_workspaces_is_independent(workspace, owner):
    other = create_workspace(name="Other Co", owner=owner)
    first = inbound(workspace, "same")
    second = inbound(other, "same")
    assert first.created and second.created and first.message.pk != second.message.pk


def test_channel_accounts_are_unique_per_workspace(workspace):
    from django.db import IntegrityError, transaction

    ChannelAccount.objects.create(
        workspace_id=workspace.pk, channel="sms", provider="twilio", external_id="+14165550000"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ChannelAccount.objects.create(
            workspace_id=workspace.pk, channel="sms", provider="twilio", external_id="+14165550000"
        )


# ---- recent tab and bulk actions ---------------------------------------------------------------


def _thread(workspace, pid, phone, when):
    inbound(workspace, pid, sender_value=phone, provider_ts=when)
    return Conversation.objects.filter(workspace=workspace).order_by("-created_at").first()


def test_recent_tab_lists_only_threads_with_recent_messages(workspace, owner, settings):
    settings.CRM_INBOX_RECENT_DAYS = 7
    old = _thread(workspace, "o1", "416-555-0101", datetime.now(UTC) - timedelta(days=30))
    new = _thread(workspace, "n1", "416-555-0102", datetime.now(UTC))
    ids = {
        c["id"]
        for c in call(login(owner), "get", "/inbox/conversations?tab=recent", workspace).json()[
            "items"
        ]
    }
    assert str(new.pk) in ids
    assert str(old.pk) not in ids


def test_bulk_actions_apply_to_many_threads(workspace, owner):
    a = _thread(workspace, "a1", "416-555-0103", T0)
    b = _thread(workspace, "b1", "416-555-0104", T0)
    admin = login(owner)
    ids = [str(a.pk), str(b.pk)]
    out = call(
        admin, "post", "/inbox/conversations/bulk", workspace, {"ids": ids, "action": "star"}
    )
    assert out.json() == {"done": 2, "skipped": 0}
    a.refresh_from_db()
    assert a.starred
    call(admin, "post", "/inbox/conversations/bulk", workspace, {"ids": ids, "action": "close"})
    b.refresh_from_db()
    assert b.status == "closed"
    call(
        admin,
        "post",
        "/inbox/conversations/bulk",
        workspace,
        {"ids": ids, "action": "assign", "user_id": str(owner.pk)},
    )
    a.refresh_from_db()
    assert a.assigned_to_id == owner.pk
    read = call(
        admin, "post", "/inbox/conversations/bulk", workspace, {"ids": ids, "action": "read"}
    )
    assert read.status_code == 200
    listed = call(admin, "get", "/inbox/conversations?tab=unread", workspace).json()["items"]
    assert listed == []


def test_bulk_rejects_bad_requests_and_other_workspaces(workspace, owner, settings):
    settings.CRM_INBOX_BULK_LIMIT = 2
    a = _thread(workspace, "x1", "416-555-0105", T0)
    admin = login(owner)
    path = "/inbox/conversations/bulk"
    bad = call(admin, "post", path, workspace, {"ids": [str(a.pk)], "action": "explode"})
    assert bad.status_code == 422
    many = call(admin, "post", path, workspace, {"ids": [str(uuid.uuid4())] * 3, "action": "star"})
    assert many.status_code == 200
    many = call(
        admin,
        "post",
        path,
        workspace,
        {"ids": [str(uuid.uuid4()) for _ in range(3)], "action": "star"},
    )
    assert many.status_code == 422
    assign_none = call(admin, "post", path, workspace, {"ids": [str(a.pk)], "action": "assign"})
    assert assign_none.status_code == 422
    other = create_workspace(name="Other Co", owner=owner)
    leaked = call(admin, "post", path, other, {"ids": [str(a.pk)], "action": "star"})
    assert leaked.json() == {"done": 0, "skipped": 1}
    a.refresh_from_db()
    assert not a.starred
    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)
    denied = call(login(viewer), "post", path, workspace, {"ids": [str(a.pk)], "action": "star"})
    assert denied.status_code == 403
    allowed = call(login(viewer), "post", path, workspace, {"ids": [str(a.pk)], "action": "read"})
    assert allowed.status_code == 200
