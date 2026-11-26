"""Reusable contract test suites for ports.

Every adapter of a port subclasses the matching suite and provides ``make_adapter``. A new adapter
is accepted only when the whole suite passes, which is what makes adapters interchangeable.
"""

from typing import Any

import pytest

from connections.models import Connection, CredentialKind
from connections.ports import SecretStore
from core.errors import NotFoundError
from notifications.ports import Notifier


class SecretStoreContract:
    """Behaviour every SecretStore must have."""

    def make_adapter(self) -> SecretStore:
        raise NotImplementedError

    @pytest.fixture
    def store(self) -> SecretStore:
        return self.make_adapter()

    def test_round_trip(self, store: SecretStore, connection: Connection) -> None:
        store.put(connection, CredentialKind.API_KEY, "s3cret-value")
        assert store.get(connection, CredentialKind.API_KEY) == "s3cret-value"

    def test_put_replaces_the_previous_value(
        self, store: SecretStore, connection: Connection
    ) -> None:
        store.put(connection, CredentialKind.ACCESS_TOKEN, "one")
        store.put(connection, CredentialKind.ACCESS_TOKEN, "two")
        assert store.get(connection, CredentialKind.ACCESS_TOKEN) == "two"

    def test_kinds_are_independent(self, store: SecretStore, connection: Connection) -> None:
        store.put(connection, CredentialKind.ACCESS_TOKEN, "access")
        store.put(connection, CredentialKind.REFRESH_TOKEN, "refresh")
        assert store.get(connection, CredentialKind.ACCESS_TOKEN) == "access"
        assert store.get(connection, CredentialKind.REFRESH_TOKEN) == "refresh"

    def test_missing_secret_raises_not_found(
        self, store: SecretStore, connection: Connection
    ) -> None:
        with pytest.raises(NotFoundError):
            store.get(connection, CredentialKind.CLIENT_SECRET)

    def test_delete_reports_whether_something_was_removed(
        self, store: SecretStore, connection: Connection
    ) -> None:
        store.put(connection, CredentialKind.API_KEY, "x")
        assert store.delete(connection, CredentialKind.API_KEY) is True
        assert store.delete(connection, CredentialKind.API_KEY) is False
        with pytest.raises(NotFoundError):
            store.get(connection, CredentialKind.API_KEY)

    def test_secrets_do_not_leak_between_connections(
        self, store: SecretStore, connection: Connection, other_connection: Connection
    ) -> None:
        store.put(connection, CredentialKind.API_KEY, "mine")
        with pytest.raises(NotFoundError):
            store.get(other_connection, CredentialKind.API_KEY)

    def test_unicode_and_long_values_survive(
        self, store: SecretStore, connection: Connection
    ) -> None:
        value = "tökén-✓-" + "x" * 5000
        store.put(connection, CredentialKind.API_KEY, value)
        assert store.get(connection, CredentialKind.API_KEY) == value


class NotifierContract:
    """Behaviour every Notifier must have."""

    def make_adapter(self) -> Notifier:
        raise NotImplementedError

    def valid_context(self) -> dict[str, Any]:
        return {"link": "https://app.example.test/reset"}

    @pytest.fixture
    def notifier(self) -> Notifier:
        return self.make_adapter()

    def test_known_template_is_accepted(self, notifier: Notifier) -> None:
        notifier.send("person@example.com", "password_reset", self.valid_context())

    def test_unknown_template_raises_key_error(self, notifier: Notifier) -> None:
        with pytest.raises(KeyError):
            notifier.send("person@example.com", "does_not_exist", {})

    def test_missing_placeholders_fail_loudly(self, notifier: Notifier) -> None:
        with pytest.raises(KeyError):
            notifier.send("person@example.com", "password_reset", {})
