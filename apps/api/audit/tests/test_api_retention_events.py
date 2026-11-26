import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.test import Client

from audit import events, outbox, retention, services
from audit.actors import Actor
from audit.models import AuditCheckpoint, AuditEvent, OutboxMessage
from conftest import make_user
from core.clock import FrozenClock
from core.housekeeping import run_housekeeping
from core.models import DeadLetter, IdempotencyRecord
from workspaces.models import Role, Workspace

pytestmark = pytest.mark.django_db
URL = "/api/v1/audit"


def _ws_headers(workspace: Workspace) -> dict[str, str]:
    return {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}


def _emit(workspace: Workspace, action: str, when: datetime | None = None, **kw) -> AuditEvent:
    return services.record_event(
        action=action,
        actor=Actor.system("t"),
        workspace_id=workspace.pk,
        clock=FrozenClock(when or datetime.now(UTC)),
        **kw,
    )


# ---- viewer API ------------------------------------------------------------------------------


def test_audit_viewer_requires_the_audit_permission(workspace: Workspace) -> None:
    _emit(workspace, "thing.happened")
    analyst = make_user("analyst@example.com", workspace, Role.ANALYST)
    viewer = make_user("viewer@example.com", workspace, Role.VIEWER)

    client = Client()
    client.force_login(viewer)
    denied = client.get(f"{URL}/events", **_ws_headers(workspace))
    assert denied.status_code == 403

    client.force_login(analyst)
    allowed = client.get(f"{URL}/events", **_ws_headers(workspace))
    assert allowed.status_code == 200
    assert any(item["action"] == "thing.happened" for item in allowed.json()["items"])


def test_audit_events_are_filtered_paginated_and_scoped_to_the_workspace(
    logged_in: Client, workspace: Workspace, owner
) -> None:
    from workspaces.services import create_workspace

    other = create_workspace(name="Other Co", owner=owner)
    _emit(other, "other.workspace.event")
    for i in range(5):
        _emit(workspace, "batch.event", target_type="thing", target_id=i)
    _emit(workspace, "single.event")

    page = logged_in.get(
        f"{URL}/events?action=batch.event&limit=2", **_ws_headers(workspace)
    ).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    following = logged_in.get(
        f"{URL}/events?action=batch.event&limit=10&cursor={page['next_cursor']}",
        **_ws_headers(workspace),
    ).json()
    assert len(following["items"]) == 3 and following["next_cursor"] is None

    everything = logged_in.get(f"{URL}/events?limit=200", **_ws_headers(workspace)).json()["items"]
    assert "other.workspace.event" not in {item["action"] for item in everything}

    unknown = logged_in.get(f"{URL}/events?limit=5&target_type=thing", **_ws_headers(workspace))
    assert unknown.status_code == 200


def test_audit_export_is_csv_and_neutralises_formula_injection(
    logged_in: Client, workspace: Workspace
) -> None:
    _emit(workspace, '=HYPERLINK("http://evil")', target_id="@cmd")
    response = logged_in.get(f"{URL}/events/export", **_ws_headers(workspace))
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    body = b"".join(response.streaming_content).decode()
    assert body.splitlines()[0].startswith("seq,occurred_at")
    assert "'=HYPERLINK" in body and "'@cmd" in body


def test_integrity_endpoint_reports_a_healthy_chain(
    logged_in: Client, workspace: Workspace
) -> None:
    _emit(workspace, "a")
    body = logged_in.get(f"{URL}/integrity", **_ws_headers(workspace)).json()
    assert body == {"intact": True, "first_broken_seq": None}


# ---- retention -------------------------------------------------------------------------------


def test_pruning_leaves_a_checkpoint_and_the_chain_still_verifies(workspace: Workspace) -> None:
    old = datetime.now(UTC) - timedelta(days=400)
    for i in range(4):
        _emit(workspace, f"old.{i}", when=old)
    _emit(workspace, "recent")

    pruned = retention.prune_audit_events(
        workspace.pk, older_than=datetime.now(UTC) - timedelta(days=30)
    )
    assert pruned >= 4
    checkpoint = AuditCheckpoint.objects.get(chain=workspace.pk)
    assert checkpoint.events_pruned == pruned
    assert not AuditEvent.objects.filter(chain=workspace.pk, action__startswith="old.").exists()
    assert services.verify_chain(workspace.pk) is None

    later = _emit(workspace, "after.prune")
    assert later.seq > checkpoint.last_seq
    assert services.verify_chain(workspace.pk) is None


def test_pruning_everything_still_allows_new_events(workspace: Workspace) -> None:
    old = datetime.now(UTC) - timedelta(days=400)
    last_old = None
    for i in range(3):
        last_old = _emit(workspace, f"old.{i}", when=old)
    retention.prune_audit_events(workspace.pk, older_than=datetime.now(UTC) - timedelta(days=30))
    ws_events = AuditEvent.objects.filter(chain=workspace.pk)
    remaining = list(ws_events.values_list("action", flat=True))
    assert last_old is not None
    fresh = _emit(workspace, "fresh")
    assert fresh.seq > last_old.seq and services.verify_chain(workspace.pk) is None
    assert "old.2" not in remaining


def test_tampering_after_pruning_is_still_detected(workspace: Workspace) -> None:
    old = datetime.now(UTC) - timedelta(days=400)
    _emit(workspace, "old", when=old)
    _emit(workspace, "keep-1")
    victim = _emit(workspace, "keep-2", after={"n": 1})
    retention.prune_audit_events(workspace.pk, older_than=datetime.now(UTC) - timedelta(days=30))
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE audit_auditevent DISABLE TRIGGER audit_event_no_update_delete")
        cursor.execute(
            "UPDATE audit_auditevent SET after = '{\"n\": 2}' WHERE id = %s", [victim.pk]
        )
        cursor.execute("ALTER TABLE audit_auditevent ENABLE TRIGGER audit_event_no_update_delete")
    assert services.verify_chain(workspace.pk) == victim.seq


def test_deletes_are_still_blocked_without_the_prune_guard(workspace: Workspace) -> None:
    from django.db import IntegrityError, transaction

    event = _emit(workspace, "protected")
    with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM audit_auditevent WHERE id = %s", [event.pk])


def test_retention_run_keeps_audit_forever_by_default_and_prunes_old_outbox(
    workspace: Workspace,
) -> None:
    _emit(workspace, "old", when=datetime.now(UTC) - timedelta(days=900))
    done = OutboxMessage.objects.create(topic="t", payload={})
    OutboxMessage.objects.filter(pk=done.pk).update(
        processed_at=datetime.now(UTC) - timedelta(days=60)
    )
    OutboxMessage.objects.create(topic="pending", payload={})

    result = retention.run_retention(audit_days=0, outbox_days=14)
    assert result == {"audit": 0, "outbox": 1}
    assert AuditEvent.objects.filter(chain=workspace.pk, action="old").exists()
    assert OutboxMessage.objects.filter(topic="pending").exists()

    result = retention.run_retention(audit_days=365, outbox_days=14)
    assert result["audit"] >= 1


def test_housekeeping_removes_stale_keys_and_resolved_dead_letters() -> None:
    stale = IdempotencyRecord.objects.create(user_id=uuid.uuid4(), key="k", request_hash="h")
    IdempotencyRecord.objects.filter(pk=stale.pk).update(
        created_at=datetime.now(UTC) - timedelta(days=30)
    )
    IdempotencyRecord.objects.create(user_id=uuid.uuid4(), key="fresh", request_hash="h")
    letter = DeadLetter.objects.create(
        task_name="t", error="e", resolved_at=datetime.now(UTC) - timedelta(days=200)
    )
    DeadLetter.objects.create(task_name="open", error="e")

    result = run_housekeeping()
    assert result == {"idempotency_records": 1, "dead_letters": 1}
    assert not DeadLetter.objects.filter(pk=letter.pk).exists()
    assert DeadLetter.objects.filter(task_name="open").exists()


# ---- event bus -------------------------------------------------------------------------------


def test_events_are_delivered_to_subscribers_through_the_outbox() -> None:
    received: list[dict[str, object]] = []
    events.subscribe(events.EventTopic.METRICS_INGESTED, received.append)
    events.publish(events.EventTopic.METRICS_INGESTED, {"batch": "a1"})
    assert received == []
    assert outbox.dispatch_batch() >= 1
    assert {"batch": "a1"} in received
