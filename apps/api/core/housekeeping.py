"""Housekeeping for core tables: stale idempotency keys and resolved dead letters."""

from datetime import timedelta

from django.utils import timezone

from core.models import DeadLetter, IdempotencyRecord

IDEMPOTENCY_TTL_DAYS = 7
RESOLVED_DEAD_LETTER_TTL_DAYS = 90


def run_housekeeping() -> dict[str, int]:
    now = timezone.now()
    stale_keys, _ = IdempotencyRecord.objects.filter(
        created_at__lt=now - timedelta(days=IDEMPOTENCY_TTL_DAYS)
    ).delete()
    resolved, _ = DeadLetter.objects.filter(
        resolved_at__isnull=False,
        resolved_at__lt=now - timedelta(days=RESOLVED_DEAD_LETTER_TTL_DAYS),
    ).delete()
    return {"idempotency_records": stale_keys, "dead_letters": resolved}
