"""Ports for credential storage. Domain code depends on these, never on the vault directly."""

from typing import Protocol

from audit.actors import Actor
from connections import services
from connections.models import Connection, Credential, CredentialKind
from core.errors import NotFoundError


class SecretStore(Protocol):
    """Write-only from outside; readable by adapters that call a platform with the secret."""

    def put(self, connection: Connection, kind: CredentialKind, plaintext: str) -> None: ...

    def get(self, connection: Connection, kind: CredentialKind) -> str:
        """Return the secret or raise ``NotFoundError`` when none is stored."""
        ...

    def delete(self, connection: Connection, kind: CredentialKind) -> bool:
        """Remove a secret; returns whether one existed."""
        ...


class VaultSecretStore:
    """Production adapter: envelope-encrypted secrets in PostgreSQL."""

    def __init__(self, actor: Actor) -> None:
        self._actor = actor

    def put(self, connection: Connection, kind: CredentialKind, plaintext: str) -> None:
        services.store_secret(connection, kind, plaintext, actor=self._actor)

    def get(self, connection: Connection, kind: CredentialKind) -> str:
        return services.read_secret(connection, kind)

    def delete(self, connection: Connection, kind: CredentialKind) -> bool:
        deleted, _ = Credential.objects.filter(connection=connection, kind=kind).delete()
        return bool(deleted)


class InMemorySecretStore:
    """Fake adapter for tests of code that only needs the port."""

    def __init__(self) -> None:
        self._data: dict[tuple[object, str], str] = {}

    def put(self, connection: Connection, kind: CredentialKind, plaintext: str) -> None:
        self._data[(connection.pk, str(kind))] = plaintext

    def get(self, connection: Connection, kind: CredentialKind) -> str:
        try:
            return self._data[(connection.pk, str(kind))]
        except KeyError as exc:
            raise NotFoundError("secret not found") from exc

    def delete(self, connection: Connection, kind: CredentialKind) -> bool:
        return self._data.pop((connection.pk, str(kind)), None) is not None
