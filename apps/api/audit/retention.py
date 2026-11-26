"""Retention: prune old audit events behind a verifiable checkpoint, and old outbox rows."""

import uuid
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.utils import timezone

from audit.models import AuditCheckpoint, AuditEvent, OutboxMessage


def prune_audit_events(chain: uuid.UUID, *, older_than: datetime) -> int:
    """Delete events of ``chain`` older than the cutoff, leaving a checkpoint at the last one.

    The database trigger blocks deletes unless ``app.audit_prune`` is set for the transaction,
    which only this function does. The chain keeps verifying from the checkpoint. Only a
    contiguous prefix (everything up to the newest event older than the cutoff) is pruned so the
    chain never has a hole.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [str(chain)])
        boundary = (
            AuditEvent.objects.filter(chain=chain, occurred_at__lt=older_than)
            .order_by("-seq")
            .first()
        )
        if boundary is None:
            return 0
        count = AuditEvent.objects.filter(chain=chain, seq__lte=boundary.seq).count()
        checkpoint, _ = AuditCheckpoint.objects.get_or_create(
            chain=chain, defaults={"last_seq": boundary.seq, "last_hash": boundary.hash}
        )
        already_pruned = checkpoint.events_pruned
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL app.audit_prune = 'on'")
            cursor.execute(
                "DELETE FROM audit_auditevent WHERE chain = %s AND seq <= %s",
                [str(chain), boundary.seq],
            )
            cursor.execute("SET LOCAL app.audit_prune = 'off'")
        checkpoint.last_seq = boundary.seq
        checkpoint.last_hash = boundary.hash
        checkpoint.events_pruned = already_pruned + count
        checkpoint.save()
        return count


def prune_outbox(*, older_than: datetime) -> int:
    deleted, _ = OutboxMessage.objects.filter(
        processed_at__isnull=False, processed_at__lt=older_than
    ).delete()
    return deleted


def run_retention(*, audit_days: int, outbox_days: int) -> dict[str, int]:
    """Apply configured retention. ``audit_days=0`` keeps audit events forever."""
    now = timezone.now()
    result = {"audit": 0, "outbox": prune_outbox(older_than=now - timedelta(days=outbox_days))}
    if audit_days > 0:
        cutoff = now - timedelta(days=audit_days)
        for chain in AuditEvent.objects.values_list("chain", flat=True).distinct():
            result["audit"] += prune_audit_events(chain, older_than=cutoff)
    return result
