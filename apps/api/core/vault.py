"""Envelope encryption for provider credentials.

Each secret is encrypted with its own random data key (AES-256-GCM). The data key is wrapped
with a versioned master key. Rotating the master key only re-wraps data keys; payloads are
never re-encrypted and the plaintext never touches the database.
"""

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

NONCE_BYTES = 12
KEY_BYTES = 32


class VaultError(RuntimeError):
    """Raised when a secret cannot be sealed or opened."""


@dataclass(frozen=True, slots=True)
class SealedSecret:
    ciphertext: bytes
    nonce: bytes
    wrapped_key: bytes
    wrapped_key_nonce: bytes
    key_version: str


def _master_key(version: str) -> bytes:
    try:
        return bytes(settings.VAULT_MASTER_KEYS[version])
    except KeyError as exc:
        raise VaultError(f"unknown master key version {version!r}") from exc


def seal(plaintext: bytes, *, aad: bytes) -> SealedSecret:
    version = str(settings.VAULT_ACTIVE_KEY_VERSION)
    data_key = AESGCM.generate_key(bit_length=KEY_BYTES * 8)
    nonce = os.urandom(NONCE_BYTES)
    wrap_nonce = os.urandom(NONCE_BYTES)
    return SealedSecret(
        ciphertext=AESGCM(data_key).encrypt(nonce, plaintext, aad),
        nonce=nonce,
        wrapped_key=AESGCM(_master_key(version)).encrypt(wrap_nonce, data_key, aad),
        wrapped_key_nonce=wrap_nonce,
        key_version=version,
    )


def _unwrap(sealed: SealedSecret, aad: bytes) -> bytes:
    try:
        return AESGCM(_master_key(sealed.key_version)).decrypt(
            sealed.wrapped_key_nonce, sealed.wrapped_key, aad
        )
    except InvalidTag as exc:
        raise VaultError("secret failed integrity verification") from exc


def open_secret(sealed: SealedSecret, *, aad: bytes) -> bytes:
    data_key = _unwrap(sealed, aad)
    try:
        return AESGCM(data_key).decrypt(sealed.nonce, sealed.ciphertext, aad)
    except InvalidTag as exc:
        raise VaultError("secret failed integrity verification") from exc


def rewrap(sealed: SealedSecret, *, aad: bytes) -> SealedSecret:
    """Re-wrap the data key under the active master key without touching the payload."""
    version = str(settings.VAULT_ACTIVE_KEY_VERSION)
    if sealed.key_version == version:
        return sealed
    data_key = _unwrap(sealed, aad)
    wrap_nonce = os.urandom(NONCE_BYTES)
    return SealedSecret(
        ciphertext=sealed.ciphertext,
        nonce=sealed.nonce,
        wrapped_key=AESGCM(_master_key(version)).encrypt(wrap_nonce, data_key, aad),
        wrapped_key_nonce=wrap_nonce,
        key_version=version,
    )
