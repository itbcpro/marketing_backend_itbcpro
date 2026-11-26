"""Audit log writes and integrity verification."""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from django.db import connection, transaction

from audit.actors import Actor
from audit.models import SYSTEM_CHAIN, AuditCheckpoint, AuditEvent
from core.clock import Clock, system_clock

GENESIS_HASH = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(event: AuditEvent) -> str:
    body = {
        "chain": str(event.chain),
        "seq": event.seq,
        "occurred_at": event.occurred_at.isoformat(),
        "workspace_id": str(event.workspace_id) if event.workspace_id else None,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "action": event.action,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "before": event.before,
        "after": event.after,
        "request_id": event.request_id,
        "prev_hash": event.prev_hash,
    }
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def record_event(
    *,
    action: str,
    actor: Actor,
    workspace_id: uuid.UUID | None = None,
    target_type: str = "",
    target_id: object = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request_id: str = "",
    ip_address: str | None = None,
    clock: Clock = system_clock,
) -> AuditEvent:
    """Append an event to the workspace chain. Must run inside the caller's transaction."""
    chain = workspace_id or SYSTEM_CHAIN
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [str(chain)])
        head_seq, head_hash = chain_head(chain)
        event = AuditEvent(
            chain=chain,
            seq=head_seq + 1,
            occurred_at=clock.now(),
            workspace_id=workspace_id,
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            before=before,
            after=after,
            request_id=request_id,
            ip_address=ip_address,
            prev_hash=head_hash,
        )
        event.hash = compute_hash(event)
        event.save(force_insert=True)
    return event


def chain_head(chain: uuid.UUID) -> tuple[int, str]:
    """Sequence number and hash of the newest event (or of the checkpoint when all were pruned)."""
    last = AuditEvent.objects.filter(chain=chain).order_by("-seq").first()
    if last:
        return int(last.seq), last.hash
    checkpoint = AuditCheckpoint.objects.filter(chain=chain).first()
    if checkpoint:
        return int(checkpoint.last_seq), checkpoint.last_hash
    return 0, GENESIS_HASH


def verify_chain(chain: uuid.UUID) -> int | None:
    """Return the sequence number of the first broken event, or ``None`` if the chain is intact."""
    checkpoint = AuditCheckpoint.objects.filter(chain=chain).first()
    prev_hash = checkpoint.last_hash if checkpoint else GENESIS_HASH
    expected_seq = (checkpoint.last_seq + 1) if checkpoint else 1
    for event in AuditEvent.objects.filter(chain=chain).order_by("seq").iterator():
        if (
            event.seq != expected_seq
            or event.prev_hash != prev_hash
            or event.hash != compute_hash(event)
        ):
            return int(event.seq)
        prev_hash = event.hash
        expected_seq += 1
    return None


def list_chains() -> list[uuid.UUID]:
    chains = set(AuditEvent.objects.values_list("chain", flat=True).distinct())
    chains |= set(AuditCheckpoint.objects.values_list("chain", flat=True))
    return sorted(chains)


def occurred_since(moment: datetime) -> int:
    return AuditEvent.objects.filter(occurred_at__gte=moment).count()
