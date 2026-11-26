import pytest
from django.test import override_settings

from audit.actors import Actor
from connections import services
from connections.models import Credential, CredentialKind
from core import vault
from workspaces.models import Workspace

KEYS = {"v1": b"0" * 32, "v2": b"1" * 32}


def test_seal_open_round_trip_and_ciphertext_hides_plaintext() -> None:
    sealed = vault.seal(b"super-secret-token", aad=b"conn:kind")
    assert b"super-secret-token" not in sealed.ciphertext
    assert vault.open_secret(sealed, aad=b"conn:kind") == b"super-secret-token"


def test_each_secret_gets_a_fresh_data_key_and_nonce() -> None:
    a, b = vault.seal(b"same", aad=b"x"), vault.seal(b"same", aad=b"x")
    assert a.ciphertext != b.ciphertext and a.wrapped_key != b.wrapped_key and a.nonce != b.nonce


def test_wrong_aad_or_tampering_is_rejected() -> None:
    sealed = vault.seal(b"secret", aad=b"a")
    with pytest.raises(vault.VaultError):
        vault.open_secret(sealed, aad=b"b")
    tampered = vault.SealedSecret(
        ciphertext=sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1]),
        nonce=sealed.nonce,
        wrapped_key=sealed.wrapped_key,
        wrapped_key_nonce=sealed.wrapped_key_nonce,
        key_version=sealed.key_version,
    )
    with pytest.raises(vault.VaultError):
        vault.open_secret(tampered, aad=b"a")


def test_unknown_key_version_is_reported() -> None:
    sealed = vault.seal(b"secret", aad=b"a")
    unknown = vault.SealedSecret(
        sealed.ciphertext, sealed.nonce, sealed.wrapped_key, sealed.wrapped_key_nonce, "v99"
    )
    with pytest.raises(vault.VaultError, match="unknown master key"):
        vault.open_secret(unknown, aad=b"a")


def test_rewrap_moves_to_active_key_without_touching_payload() -> None:
    sealed = vault.seal(b"secret", aad=b"a")
    assert sealed.key_version == "v1"
    with override_settings(VAULT_ACTIVE_KEY_VERSION="v2", VAULT_MASTER_KEYS=KEYS):
        rewrapped = vault.rewrap(sealed, aad=b"a")
        assert rewrapped.key_version == "v2"
        assert rewrapped.ciphertext == sealed.ciphertext
        assert vault.open_secret(rewrapped, aad=b"a") == b"secret"
        assert vault.rewrap(rewrapped, aad=b"a") is rewrapped


@pytest.mark.django_db
def test_store_read_and_rotate_credentials(workspace: Workspace) -> None:
    actor = Actor.system("test")
    connection = services.create_connection(
        workspace_id=workspace.pk,
        provider="meta",
        external_account_id="act_123",
        display_name="Main account",
        actor=actor,
    )
    services.store_secret(connection, CredentialKind.ACCESS_TOKEN, "tok-abc", actor=actor)
    raw = Credential.objects.get(connection=connection)
    assert b"tok-abc" not in bytes(raw.ciphertext)
    assert services.read_secret(connection, CredentialKind.ACCESS_TOKEN) == "tok-abc"

    with override_settings(VAULT_ACTIVE_KEY_VERSION="v2", VAULT_MASTER_KEYS=KEYS):
        assert services.rotate_master_key() == 1
        raw.refresh_from_db()
        assert raw.key_version == "v2"
        assert services.read_secret(connection, CredentialKind.ACCESS_TOKEN) == "tok-abc"
        assert services.rotate_master_key() == 0


@pytest.mark.django_db
def test_credential_cannot_be_moved_to_another_connection(workspace: Workspace) -> None:
    actor = Actor.system("test")
    one = services.create_connection(
        workspace_id=workspace.pk,
        provider="meta",
        external_account_id="1",
        display_name="",
        actor=actor,
    )
    two = services.create_connection(
        workspace_id=workspace.pk,
        provider="meta",
        external_account_id="2",
        display_name="",
        actor=actor,
    )
    services.store_secret(one, CredentialKind.API_KEY, "k", actor=actor)
    Credential.objects.filter(connection=one).update(connection=two)
    with pytest.raises(vault.VaultError):
        services.read_secret(two, CredentialKind.API_KEY)
