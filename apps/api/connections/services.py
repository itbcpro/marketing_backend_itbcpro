"""Connection and credential use cases."""

import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit import events
from audit.actors import Actor
from audit.services import record_event
from connections.models import Connection, ConnectionStatus, Credential, CredentialKind
from core import vault
from core.errors import NotFoundError


def _aad(connection_id: uuid.UUID, kind: str) -> bytes:
    return f"{connection_id}:{kind}".encode()


def create_connection(
    *,
    workspace_id: uuid.UUID,
    provider: str,
    external_account_id: str,
    display_name: str,
    actor: Actor,
    requested_scopes: list[str] | None = None,
    api_version: str = "",
) -> Connection:
    with transaction.atomic():
        connection = Connection.objects.create(
            workspace_id=workspace_id,
            provider=provider,
            external_account_id=external_account_id,
            display_name=display_name,
            requested_scopes=requested_scopes or [],
            api_version=api_version,
        )
        record_event(
            action="connection.created",
            actor=actor,
            workspace_id=workspace_id,
            target_type="connection",
            target_id=connection.pk,
            after={"provider": provider, "external_account_id": external_account_id},
        )
    return connection


def store_secret(
    connection: Connection, kind: CredentialKind, plaintext: str, *, actor: Actor
) -> Credential:
    sealed = vault.seal(plaintext.encode(), aad=_aad(connection.pk, kind))
    with transaction.atomic():
        credential, _ = Credential.objects.update_or_create(
            connection=connection,
            kind=kind,
            defaults={
                "workspace_id": connection.workspace_id,
                "ciphertext": sealed.ciphertext,
                "nonce": sealed.nonce,
                "wrapped_key": sealed.wrapped_key,
                "wrapped_key_nonce": sealed.wrapped_key_nonce,
                "key_version": sealed.key_version,
                "rotated_at": timezone.now(),
            },
        )
        record_event(
            action="credential.stored",
            actor=actor,
            workspace_id=connection.workspace_id,
            target_type="connection",
            target_id=connection.pk,
            after={"kind": str(kind), "key_version": sealed.key_version},
        )
    return credential


def _sealed_from(credential: Credential) -> vault.SealedSecret:
    return vault.SealedSecret(
        ciphertext=bytes(credential.ciphertext),
        nonce=bytes(credential.nonce),
        wrapped_key=bytes(credential.wrapped_key),
        wrapped_key_nonce=bytes(credential.wrapped_key_nonce),
        key_version=credential.key_version,
    )


def read_secret(connection: Connection, kind: CredentialKind) -> str:
    try:
        credential = Credential.objects.get(connection=connection, kind=kind)
    except Credential.DoesNotExist as exc:
        raise NotFoundError(f"no {kind} stored for connection") from exc
    return vault.open_secret(_sealed_from(credential), aad=_aad(connection.pk, kind)).decode()


def rotate_master_key(batch_size: int = 200) -> int:
    """Re-wrap every credential under the active master key. Returns the number rewrapped."""
    rewrapped = 0
    stale = Credential.objects.exclude(key_version=settings.VAULT_ACTIVE_KEY_VERSION)
    for credential in stale.iterator(chunk_size=batch_size):
        updated = vault.rewrap(
            _sealed_from(credential), aad=_aad(credential.connection_id, credential.kind)
        )
        credential.wrapped_key = updated.wrapped_key
        credential.wrapped_key_nonce = updated.wrapped_key_nonce
        credential.key_version = updated.key_version
        credential.rotated_at = timezone.now()
        credential.save(
            update_fields=[
                "wrapped_key",
                "wrapped_key_nonce",
                "key_version",
                "rotated_at",
                "updated_at",
            ]
        )
        rewrapped += 1
    return rewrapped


def set_status(connection: Connection, status: ConnectionStatus, detail: str = "") -> None:
    previous = connection.status
    connection.status = status
    connection.health_detail = detail[:500]
    connection.last_health_at = timezone.now()
    connection.save(update_fields=["status", "health_detail", "last_health_at", "updated_at"])
    if previous != status:
        record_event(
            action="connection.status_changed",
            actor=Actor.system("connections"),
            workspace_id=connection.workspace_id,
            target_type="connection",
            target_id=connection.pk,
            before={"status": previous},
            after={"status": str(status), "detail": detail[:200]},
        )
        events.publish(
            events.EventTopic.CONNECTION_STATUS_CHANGED,
            {
                "connection_id": str(connection.pk),
                "workspace_id": str(connection.workspace_id),
                "provider": connection.provider,
                "from": previous,
                "to": str(status),
            },
        )


def destroy_credentials(connection: Connection, *, actor: Actor) -> int:
    """Permanently delete every stored secret of a connection."""
    deleted, _ = Credential.objects.filter(connection=connection).delete()
    record_event(
        action="credential.destroyed",
        actor=actor,
        workspace_id=connection.workspace_id,
        target_type="connection",
        target_id=connection.pk,
        after={"credentials": deleted},
    )
    return deleted
