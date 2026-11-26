import uuid
from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, connection, transaction

from audit import outbox, services
from audit.actors import Actor
from audit.models import SYSTEM_CHAIN, AuditEvent, OutboxMessage
from core.clock import FrozenClock

pytestmark = pytest.mark.django_db


def _event(action: str, workspace_id: uuid.UUID | None = None, **kw: object) -> AuditEvent:
    return services.record_event(
        action=action,
        actor=Actor.system("test"),
        workspace_id=workspace_id,
        clock=FrozenClock(datetime(2026, 1, 1, tzinfo=UTC)),
        **kw,  # type: ignore[arg-type]
    )


def test_events_chain_by_sequence_and_hash() -> None:
    ws = uuid.uuid4()
    first, second = _event("a", ws), _event("b", ws)
    assert (first.seq, second.seq) == (1, 2)
    assert first.prev_hash == services.GENESIS_HASH
    assert second.prev_hash == first.hash
    assert services.verify_chain(ws) is None


def test_chains_are_isolated_per_workspace_and_system() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    _event("x", a)
    _event("y", b)
    system = _event("z")
    assert system.chain == SYSTEM_CHAIN and system.seq == 1
    assert AuditEvent.objects.filter(chain=a).count() == 1


def test_update_and_delete_are_blocked_by_database_triggers() -> None:
    event = _event("immutable")
    for statement in (
        "UPDATE audit_auditevent SET action = 'tampered' WHERE id = %s",
        "DELETE FROM audit_auditevent WHERE id = %s",
    ):
        with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(statement, [event.pk])
    with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE audit_auditevent")


def test_verify_chain_detects_tampering_when_triggers_are_bypassed() -> None:
    ws = uuid.uuid4()
    _event("one", ws)
    victim = _event("two", ws, after={"amount": 1})
    _event("three", ws)
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE audit_auditevent DISABLE TRIGGER audit_event_no_update_delete")
        cursor.execute(
            "UPDATE audit_auditevent SET after = '{\"amount\": 999}' WHERE id = %s", [victim.pk]
        )
        cursor.execute("ALTER TABLE audit_auditevent ENABLE TRIGGER audit_event_no_update_delete")
    assert services.verify_chain(ws) == 2


def test_verify_chain_detects_a_removed_event() -> None:
    ws = uuid.uuid4()
    _event("one", ws)
    middle = _event("two", ws)
    _event("three", ws)
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE audit_auditevent DISABLE TRIGGER audit_event_no_update_delete")
        cursor.execute("DELETE FROM audit_auditevent WHERE id = %s", [middle.pk])
        cursor.execute("ALTER TABLE audit_auditevent ENABLE TRIGGER audit_event_no_update_delete")
    assert services.verify_chain(ws) == 3


def test_outbox_delivers_once_and_retries_failures_with_backoff() -> None:
    seen: list[dict[str, object]] = []
    outbox.register_handler("test.ok", seen.append)

    def boom(_: dict[str, object]) -> None:
        raise RuntimeError("handler exploded")

    outbox.register_handler("test.fail", boom)
    outbox.enqueue("test.ok", {"n": 1})
    failing = outbox.enqueue("test.fail", {"n": 2})

    assert outbox.dispatch_batch() == 1
    assert seen == [{"n": 1}]
    assert outbox.dispatch_batch() == 0  # already delivered; failing one is backing off

    failing.refresh_from_db()
    assert failing.attempts == 1 and "handler exploded" in failing.last_error
    assert failing.processed_at is None
    assert OutboxMessage.objects.filter(processed_at__isnull=False).count() == 1
