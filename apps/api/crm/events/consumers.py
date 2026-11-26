"""Idempotent consumers for outbox events."""

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.db import transaction

from core import tenancy
from crm.events.envelope import EventEnvelope, parse
from crm.events.models import ProcessedEvent

Consumer = Callable[[EventEnvelope], None]


def idempotent(consumer_name: str) -> Callable[[Consumer], Callable[[dict[str, Any]], None]]:
    """Wrap a consumer as an outbox handler.

    The handler runs in the event's tenant context and in one transaction with its
    ``ProcessedEvent`` mark: a failure rolls both back (the outbox retries) and a
    redelivery after success is skipped.
    """

    def decorate(func: Consumer) -> Callable[[dict[str, Any]], None]:
        @wraps(func)
        def handler(payload: dict[str, Any]) -> None:
            envelope = parse(payload)
            with tenancy.use_workspace(envelope.tenant_id), transaction.atomic():
                _, created = ProcessedEvent.objects.get_or_create(
                    workspace_id=envelope.tenant_id,
                    consumer=consumer_name,
                    event_id=envelope.id,
                )
                if created:
                    func(envelope)

        return handler

    return decorate
