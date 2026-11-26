import uuid

import pytest

from audit.actors import Actor
from connections import services as connection_services
from connections.models import ConnectionStatus
from core.errors import AuthExpiredError, PermanentProviderError, RetryableProviderError
from crm.channels.instagram_dm import InstagramAdapter
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.contacts.models import IdentityChannel
from crm.inbox import services as inbox
from crm.inbox.models import Message
from crm.sending import gateway, registry
from social.instagram import factory
from social.models import SocialAccount

pytestmark = pytest.mark.django_db
SYSTEM = Actor.system("test")
PAGE = "17841400000000001"


class FakeClient:
    def __init__(self, outcome=None):
        self.sent: list[tuple[str, str]] = []
        self.outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, recipient_id, text):
        if self.outcome:
            raise self.outcome
        self.sent.append((recipient_id, text))
        return {"message_id": f"mid.{uuid.uuid4().hex[:8]}"}


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(factory, "client_factory", lambda connection: fake)
    registry.register("instagram", InstagramAdapter)
    return fake


@pytest.fixture
def account(workspace):
    connection = connection_services.create_connection(
        workspace_id=workspace.pk,
        provider="instagram",
        external_account_id=PAGE,
        display_name="@acme",
        actor=SYSTEM,
    )
    connection_services.set_status(connection, ConnectionStatus.ACTIVE)
    return SocialAccount.objects.create(
        workspace_id=workspace.pk, connection=connection, external_id=PAGE, username="acme"
    )


def open_window(workspace, account):
    """A customer message arrives: it creates the contact and opens the 24 hour window."""
    from datetime import UTC, datetime

    result = inbox.ingest(
        workspace_id=workspace.pk,
        item=inbox.InboundMessage(
            channel="instagram",
            provider_message_id="mid.in1",
            sender_channel=IdentityChannel.INSTAGRAM,
            sender_value="IGSID1",
            provider_ts=datetime.now(UTC),
            body="Hi",
        ),
    )
    consent.record(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=result.contact.pk,
        channel="instagram",
        purpose="transactional",
        status="opted_in",
        source="inbound_message",
    )
    return result.contact


def send(workspace, contact):
    return gateway.send(
        workspace_id=workspace.pk,
        actor=SYSTEM,
        contact_id=contact.pk,
        channel="instagram",
        body="Thanks for writing",
    )


def test_a_reply_goes_out_through_the_social_connection(workspace, account, client):
    contact = open_window(workspace, account)
    message = Message.objects.get(pk=send(workspace, contact).message.pk)
    assert message.status == "sent" and message.provider_message_id.startswith("mid.")
    assert client.sent == [("IGSID1", "Thanks for writing")]


def test_outside_the_window_instagram_is_blocked_by_the_gateway(workspace, account, client):
    contact = contacts.create_contact(workspace_id=workspace.pk, actor=SYSTEM, data={})
    outcome = send(workspace, contact)
    assert outcome.blocked and client.sent == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AuthExpiredError("expired"), "instagram_token_expired"),
        (PermanentProviderError("no"), "instagram_rejected"),
    ],
)
def test_provider_problems_become_stable_codes(workspace, account, client, error, code):
    contact = open_window(workspace, account)
    client.outcome = error
    message = Message.objects.get(pk=send(workspace, contact).message.pk)
    assert (message.status, message.error_code) == ("failed", code)


def test_temporary_trouble_asks_for_a_retry(workspace, account, client):
    from crm.sending.ports import OutboundRequest, RetryableAdapterError

    client.outcome = RetryableProviderError("slow")
    request = OutboundRequest(
        workspace_id=workspace.pk,
        message_id=uuid.uuid4(),
        channel="instagram",
        to="IGSID1",
        body="Hi",
    )
    with pytest.raises(RetryableAdapterError):
        InstagramAdapter().send(request)


def test_without_a_connected_account_the_send_fails_clearly(workspace, client):
    contact = open_window(workspace, None)
    message = Message.objects.get(pk=send(workspace, contact).message.pk)
    assert message.error_code == "instagram_not_connected"
