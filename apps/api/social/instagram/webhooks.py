"""Instagram webhooks: verify the call, store each event once, then trigger an inbox cycle.

Every delivery is signed with the app secret (``X-Hub-Signature-256``); a call that fails the
signature is rejected before anything is read. Events are stored before they are processed and
duplicates (Instagram delivers at least once) are dropped by a content hash.
"""

import hashlib
import hmac
import json
from functools import partial
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction

from connections.models import Connection, CredentialKind
from connections.services import read_secret
from core.errors import DomainError
from social.instagram.credentials import APP_PROVIDER
from social.models import SocialAccount, WebhookEvent

SIGNATURE_PREFIX = "sha256="
DEBOUNCE_SECONDS = 20


def _app_secrets() -> list[str]:
    secrets = [settings.INSTAGRAM_APP_SECRET] if settings.INSTAGRAM_APP_SECRET else []
    for connection in Connection.objects.filter(provider=APP_PROVIDER).exclude(status="revoked"):
        try:
            secrets.append(read_secret(connection, CredentialKind.CLIENT_SECRET))
        except DomainError:
            continue
    return secrets


def verify_signature(body: bytes, header: str) -> bool:
    if not header.startswith(SIGNATURE_PREFIX):
        return False
    given = header.removeprefix(SIGNATURE_PREFIX)
    return any(
        hmac.compare_digest(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), given)
        for secret in _app_secrets()
    )


def verify_challenge(mode: str, token: str, challenge: str) -> str | None:
    """The answer to Instagram's subscription handshake, or ``None`` when it must be refused."""
    expected = settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return challenge
    return None


def _events(payload: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """``(account id, field, value)`` for every change and messaging event of a delivery."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for entry in payload.get("entry", []):
        account_id = str(entry.get("id", ""))
        found.extend(
            (account_id, str(c.get("field", "")), dict(c.get("value") or {}))
            for c in entry.get("changes", [])
        )
        found.extend((account_id, "messages", dict(m)) for m in entry.get("messaging", []))
    return found


def _store(account: SocialAccount, field: str, value: dict[str, Any]) -> bool:
    key = hashlib.sha256(
        f"{account.external_id}|{field}|{json.dumps(value, sort_keys=True)}".encode()
    ).hexdigest()
    try:
        with transaction.atomic():
            WebhookEvent.objects.create(
                workspace_id=account.workspace_id,
                account=account,
                dedupe_key=key,
                field=field[:40],
                payload=value,
            )
    except IntegrityError:
        return False
    return True


def ingest(payload: dict[str, Any]) -> int:
    """Store the new events of a delivery and queue one inbox cycle per touched account."""
    from social.tasks import inbox_cycle_task

    stored = 0
    touched: set[str] = set()
    for account_id, field, value in _events(payload):
        account = SocialAccount.objects.filter(external_id=account_id).first()
        if account is None:
            continue
        if _store(account, field, value):
            stored += 1
            touched.add(str(account.pk))
    for account_pk in touched:
        if cache.add(f"social-inbox-cycle:{account_pk}", 1, DEBOUNCE_SECONDS):
            transaction.on_commit(partial(inbox_cycle_task.delay, account_pk))
    return stored
