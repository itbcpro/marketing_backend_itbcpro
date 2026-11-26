import pytest
from django.core import mail

from audit.actors import Actor
from conftest_contracts import NotifierContract, SecretStoreContract
from connections import services
from connections.models import Connection
from connections.ports import InMemorySecretStore, SecretStore, VaultSecretStore
from notifications.ports import EmailNotifier, Notifier, RecordingNotifier
from workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _make(workspace: Workspace, account: str) -> Connection:
    return services.create_connection(
        workspace_id=workspace.pk,
        provider="contract",
        external_account_id=account,
        display_name=account,
        actor=Actor.system("test"),
    )


@pytest.fixture
def connection(workspace: Workspace) -> Connection:
    return _make(workspace, "one")


@pytest.fixture
def other_connection(workspace: Workspace) -> Connection:
    return _make(workspace, "two")


class TestVaultSecretStore(SecretStoreContract):
    def make_adapter(self) -> SecretStore:
        return VaultSecretStore(Actor.system("contract"))


class TestInMemorySecretStore(SecretStoreContract):
    def make_adapter(self) -> SecretStore:
        return InMemorySecretStore()


class TestEmailNotifier(NotifierContract):
    def make_adapter(self) -> Notifier:
        return EmailNotifier()

    def test_email_is_actually_delivered(self, notifier: Notifier) -> None:
        notifier.send("person@example.com", "password_reset", self.valid_context())
        assert mail.outbox[-1].to == ["person@example.com"]
        assert "https://app.example.test/reset" in mail.outbox[-1].body


class TestRecordingNotifier(NotifierContract):
    def make_adapter(self) -> Notifier:
        return RecordingNotifier()
