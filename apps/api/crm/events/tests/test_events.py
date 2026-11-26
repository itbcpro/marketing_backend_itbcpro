import uuid

import pytest
from django.db import connection, transaction
from pydantic import BaseModel, ValidationError

from audit import outbox
from audit.actors import Actor
from audit.models import OutboxMessage
from audit.outbox import dispatch_batch, register_handler
from core import tenancy
from crm.events import registry
from crm.events.consumers import idempotent
from crm.events.envelope import EventEnvelope, parse
from crm.events.models import ProcessedEvent
from crm.events.publisher import publish

pytestmark = pytest.mark.django_db
TYPE = "crm_test.thing_happened"


@pytest.fixture(autouse=True)
def _event_type():
    class ThingHappened(BaseModel):
        name: str

    registry._registry[(TYPE, 1)] = ThingHappened
    yield ThingHappened
    registry._registry.pop((TYPE, 1), None)


def test_publish_enqueues_a_validated_envelope(workspace, _event_type):
    assert connection.in_atomic_block
    envelope = publish(
        TYPE, workspace_id=workspace.pk, actor=Actor.system("test"), data=_event_type(name="x")
    )
    assert OutboxMessage.objects.filter(topic=TYPE, payload__id=str(envelope.id)).exists()
    assert envelope.tenant_id == workspace.pk and envelope.version == 1
    assert envelope.occurred_at.tzinfo is not None


def test_publish_refuses_to_run_outside_a_transaction(_event_type, monkeypatch):
    monkeypatch.setattr(connection, "in_atomic_block", False)
    with pytest.raises(RuntimeError, match="transaction"):
        publish(TYPE, workspace_id=uuid.uuid4(), actor=Actor.system(), data=_event_type(name="x"))


def test_publish_rejects_wrong_data_type_and_unknown_events(workspace, _event_type):
    class Other(BaseModel):
        name: str

    with pytest.raises(TypeError):
        publish(TYPE, workspace_id=workspace.pk, actor=Actor.system(), data=Other(name="x"))
    with pytest.raises(KeyError):
        publish(
            "crm_test.unknown",
            workspace_id=workspace.pk,
            actor=Actor.system(),
            data=Other(name="x"),
        )


def test_duplicate_registration_is_rejected():
    with pytest.raises(ValueError, match="already registered"):

        @registry.event(TYPE, 1)
        class Again(BaseModel):
            name: str


def test_envelope_rejects_bad_types_and_naive_times():
    good = {
        "id": str(uuid.uuid4()),
        "type": TYPE,
        "version": 1,
        "tenant_id": str(uuid.uuid4()),
        "occurred_at": "2026-11-18T10:00:00+00:00",
        "actor": {"type": "system", "id": "x"},
        "data": {},
    }
    assert isinstance(parse(good), EventEnvelope)
    for bad in (
        {**good, "type": "NoDots"},
        {**good, "occurred_at": "2026-11-18T10:00:00"},
        {**good, "version": 0},
        {**good, "extra": 1},
    ):
        with pytest.raises(ValidationError):
            parse(bad)


def test_consumer_runs_once_even_when_delivered_twice(workspace, _event_type):
    seen: list[uuid.UUID | None] = []

    @idempotent("test-consumer")
    def consume(envelope: EventEnvelope) -> None:
        seen.append(tenancy.current_workspace_id())

    envelope = publish(
        TYPE, workspace_id=workspace.pk, actor=Actor.system(), data=_event_type(name="x")
    )
    payload = envelope.model_dump(mode="json")
    consume(payload)
    consume(payload)
    assert seen == [workspace.pk]  # ran once, inside the event's tenant
    assert ProcessedEvent.objects.filter(event_id=envelope.id).count() == 1
    assert tenancy.current_workspace_id() is None


def test_failed_consumer_leaves_no_mark_so_it_is_retried(workspace, _event_type):
    calls = {"n": 0}

    @idempotent("flaky-consumer")
    def consume(envelope: EventEnvelope) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    envelope = publish(
        TYPE, workspace_id=workspace.pk, actor=Actor.system(), data=_event_type(name="x")
    )
    payload = envelope.model_dump(mode="json")
    with pytest.raises(RuntimeError), transaction.atomic():
        consume(payload)
    consume(payload)
    assert calls["n"] == 2
    assert ProcessedEvent.objects.filter(consumer="flaky-consumer").count() == 1


def test_outbox_delivers_to_an_idempotent_handler(workspace, _event_type, request):
    received: list[str] = []

    @idempotent("outbox-consumer")
    def consume(envelope: EventEnvelope) -> None:
        received.append(envelope.data["name"])

    register_handler(TYPE, consume)
    request.addfinalizer(lambda: outbox._handlers.pop(TYPE, None))
    publish(TYPE, workspace_id=workspace.pk, actor=Actor.system(), data=_event_type(name="hello"))
    assert dispatch_batch() >= 1
    assert received == ["hello"]
