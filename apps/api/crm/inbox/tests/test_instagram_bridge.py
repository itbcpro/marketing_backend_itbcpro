import uuid
from datetime import UTC, datetime, timedelta

import pytest

from crm.contacts import services as contacts
from crm.inbox import instagram_bridge, tasks
from crm.inbox.models import BridgeCursor, ChannelAccount, Conversation, Message
from social.models import SocialConversation, SocialMessage
from workspaces.services import create_workspace

pytestmark = pytest.mark.django_db
T0 = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)


def dm(
    account,
    person="igsid-7",
    username="zed",
    text="hello",
    inbound=True,
    minutes=0,
    external=None,
    author="user",
):
    thread, _ = SocialConversation.objects.get_or_create(
        workspace_id=account.workspace_id,
        account=account,
        external_id=f"thread-{person}",
        defaults={"participant_id": person, "participant_username": username},
    )
    return SocialMessage.objects.create(
        workspace_id=account.workspace_id,
        conversation=thread,
        external_id=external or f"mid-{uuid.uuid4()}",
        inbound=inbound,
        author=author if not inbound else "user",
        text=text,
        sent_at=T0 + timedelta(minutes=minutes),
    )


def test_inbound_and_outbound_dms_land_in_one_thread_of_one_contact(workspace, account):
    dm(account, text="Do you ship to Ottawa?", minutes=0)
    dm(account, text="Yes, we do.", inbound=False, author="owner", minutes=2)
    dm(account, text="Great, thanks", minutes=5)
    result = instagram_bridge.run(workspace.pk)
    assert result == {"seen": 3, "copied": 3}
    conversation = Conversation.objects.get()
    contact = conversation.contact
    assert (
        contact.first_name == "zed"
        and contacts.find_contact(workspace.pk, "instagram", "igsid-7").pk == contact.pk
    )
    messages = list(Message.objects.filter(conversation=conversation).order_by("provider_ts"))
    assert [(m.direction, m.channel, m.body) for m in messages] == [
        ("inbound", "instagram", "Do you ship to Ottawa?"),
        ("outbound", "instagram", "Yes, we do."),
        ("inbound", "instagram", "Great, thanks"),
    ]
    assert conversation.last_preview == "Great, thanks"
    assert ChannelAccount.objects.get().display_name == "@acme_it"
    assert {m.channel_account_id for m in messages} == {ChannelAccount.objects.get().pk}


def test_running_again_copies_only_what_is_new(workspace, account):
    dm(account, text="one", minutes=0)
    assert instagram_bridge.run(workspace.pk) == {"seen": 1, "copied": 1}
    assert instagram_bridge.run(workspace.pk) == {"seen": 0, "copied": 0}
    dm(account, text="two", minutes=1)
    assert instagram_bridge.run(workspace.pk) == {"seen": 1, "copied": 1}
    assert Message.objects.count() == 2


def test_a_lost_cursor_cannot_duplicate_messages(workspace, account):
    dm(account, text="one")
    dm(account, text="two", minutes=1)
    instagram_bridge.run(workspace.pk)
    BridgeCursor.objects.all().delete()
    assert instagram_bridge.run(workspace.pk) == {"seen": 2, "copied": 0}
    assert Message.objects.count() == 2


def test_chunking_follows_creation_order(workspace, account, settings):
    settings.CRM_BULK_CHUNK_SIZE = 2
    for i in range(5):
        dm(account, text=f"m{i}", minutes=i)
    assert instagram_bridge.run(workspace.pk) == {"seen": 5, "copied": 5}
    assert Conversation.objects.get().last_preview == "m4"


def test_a_backfilled_older_message_does_not_rewrite_the_summary(workspace, account):
    dm(account, text="newest", minutes=30)
    instagram_bridge.run(workspace.pk)
    dm(account, text="older", minutes=1)
    instagram_bridge.run(workspace.pk)
    conversation = Conversation.objects.get()
    assert conversation.last_preview == "newest"
    assert Message.objects.count() == 2


def test_messages_without_a_person_or_id_are_skipped(workspace, account):
    thread = SocialConversation.objects.create(
        workspace_id=workspace.pk, account=account, external_id="", participant_id=""
    )
    SocialMessage.objects.create(
        workspace_id=workspace.pk,
        conversation=thread,
        external_id="x",
        inbound=True,
        text="?",
        sent_at=T0,
    )
    assert instagram_bridge.run(workspace.pk) == {"seen": 1, "copied": 0}
    assert not Message.objects.exists()


def test_two_people_get_two_contacts_and_threads(workspace, account):
    dm(account, person="a", username="ann")
    dm(account, person="b", username="bob")
    instagram_bridge.run(workspace.pk)
    assert Conversation.objects.count() == 2
    assert sorted(Conversation.objects.values_list("contact__first_name", flat=True)) == [
        "ann",
        "bob",
    ]


def test_another_workspace_is_untouched(workspace, account, owner):
    other = create_workspace(name="Other Co", owner=owner)
    dm(account)
    assert instagram_bridge.run(other.pk) == {"seen": 0, "copied": 0}
    assert not Conversation.objects.for_workspace(other).exists()


def test_the_task_fans_out_per_workspace_and_runs_in_its_tenant(workspace, account):
    dm(account, text="via task")
    assert tasks.bridge_instagram_all.apply().get() == 1
    assert Conversation.objects.get().last_preview == "via task"
    assert instagram_bridge.workspaces_with_instagram() == [workspace.pk]
