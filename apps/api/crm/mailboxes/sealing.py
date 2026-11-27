"""Seal and open small JSON secrets (SMTP passwords, OAuth tokens) with the vault."""

from __future__ import annotations

import json
from typing import Any

from core import vault
from crm.mailboxes.models import AuthRequest, Mailbox


def _aad(kind: str, workspace_id: Any, pk: Any) -> bytes:
    return f"crm.mailbox.{kind}:{workspace_id}:{pk}".encode()


def seal_credentials(mailbox: Mailbox, credentials: dict[str, Any]) -> None:
    """Replace the sealed credentials of ``mailbox`` (the caller saves it)."""
    sealed = vault.seal(
        json.dumps(credentials).encode(), aad=_aad("secret", mailbox.workspace_id, mailbox.pk)
    )
    mailbox.secret_ciphertext = sealed.ciphertext
    mailbox.secret_nonce = sealed.nonce
    mailbox.secret_wrapped_key = sealed.wrapped_key
    mailbox.secret_wrapped_key_nonce = sealed.wrapped_key_nonce
    mailbox.secret_key_version = sealed.key_version


def open_credentials(mailbox: Mailbox) -> dict[str, Any]:
    if not bytes(mailbox.secret_ciphertext):
        return {}
    sealed = vault.SealedSecret(
        ciphertext=bytes(mailbox.secret_ciphertext),
        nonce=bytes(mailbox.secret_nonce),
        wrapped_key=bytes(mailbox.secret_wrapped_key),
        wrapped_key_nonce=bytes(mailbox.secret_wrapped_key_nonce),
        key_version=mailbox.secret_key_version,
    )
    opened = vault.open_secret(sealed, aad=_aad("secret", mailbox.workspace_id, mailbox.pk))
    data = json.loads(opened)
    return data if isinstance(data, dict) else {}


def wipe_credentials(mailbox: Mailbox) -> None:
    mailbox.secret_ciphertext = b""
    mailbox.secret_nonce = b""
    mailbox.secret_wrapped_key = b""
    mailbox.secret_wrapped_key_nonce = b""
    mailbox.secret_key_version = ""


def seal_verifier(request: AuthRequest, verifier: str) -> None:
    sealed = vault.seal(verifier.encode(), aad=_aad("verifier", request.workspace_id, request.pk))
    request.verifier_ciphertext = sealed.ciphertext
    request.verifier_nonce = sealed.nonce
    request.verifier_wrapped_key = sealed.wrapped_key
    request.verifier_wrapped_key_nonce = sealed.wrapped_key_nonce
    request.verifier_key_version = sealed.key_version


def open_verifier(request: AuthRequest) -> str:
    sealed = vault.SealedSecret(
        ciphertext=bytes(request.verifier_ciphertext),
        nonce=bytes(request.verifier_nonce),
        wrapped_key=bytes(request.verifier_wrapped_key),
        wrapped_key_nonce=bytes(request.verifier_wrapped_key_nonce),
        key_version=request.verifier_key_version,
    )
    return vault.open_secret(
        sealed, aad=_aad("verifier", request.workspace_id, request.pk)
    ).decode()
