"""Transactional outbox: enqueue in the caller's transaction, dispatch afterwards."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from audit.models import OutboxMessage
from core.logging import get_logger

Handler = Callable[[dict[str, Any]], None]

MAX_ATTEMPTS = 10
_handlers: dict[str, list[Handler]] = {}
log = get_logger(__name__)


def register_handler(topic: str, handler: Handler) -> None:
    _handlers.setdefault(topic, []).append(handler)


def enqueue(topic: str, payload: dict[str, Any]) -> OutboxMessage:
    """Write a message in the current transaction; it becomes visible only on commit."""
    return OutboxMessage.objects.create(topic=topic, payload=payload)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(2**attempts, 3600))


def dispatch_batch(limit: int = 100) -> int:
    """Deliver due messages to their handlers. Handlers must be idempotent."""
    delivered = 0
    with transaction.atomic():
        due = (
            OutboxMessage.objects.select_for_update(skip_locked=True)
            .filter(processed_at__isnull=True, available_at__lte=timezone.now())
            .filter(attempts__lt=MAX_ATTEMPTS)
            .order_by("available_at")[:limit]
        )
        for message in due:
            errors: list[str] = []
            for handler in _handlers.get(message.topic, []):
                try:
                    handler(message.payload)
                except Exception as exc:
                    errors.append(
                        f"{getattr(handler, '__name__', 'handler')}: {type(exc).__name__}: {exc}"
                    )
            if errors:
                message.attempts += 1
                message.last_error = "; ".join(errors)[:2000]
                message.available_at = timezone.now() + _backoff(message.attempts)
                log.warning("outbox_handler_failed", topic=message.topic, attempts=message.attempts)
            else:
                message.processed_at = timezone.now()
                delivered += 1
            message.save(update_fields=["processed_at", "attempts", "last_error", "available_at"])
    return delivered
