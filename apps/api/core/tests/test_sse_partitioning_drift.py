import uuid
from datetime import date

import httpx
import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test import AsyncClient, Client, override_settings

from audit.models import AuditEvent, OutboxMessage
from connections import drift
from connections.models import DocSnapshot
from core import sse
from core.partitioning import (
    PartitionedTable,
    drop_expired_partitions,
    ensure_partitions,
    maintain_all,
    partition_name,
    register_partitioned_table,
)
from workspaces.models import Workspace

pytestmark = pytest.mark.django_db


# ---- SSE --------------------------------------------------------------------------------------


def test_events_are_delivered_in_order_and_resumable() -> None:
    channel = f"test-{uuid.uuid4()}"
    first = sse.publish(channel, "run.started", {"n": 1})
    second = sse.publish(channel, "run.finished", {"n": 2})

    async def collect(last_id: str | None, wanted: int) -> list[str]:
        frames: list[str] = []
        async for frame in sse.stream(channel, last_event_id=last_id, block_ms=200):
            if frame.startswith("id:"):
                frames.append(frame)
            if len(frames) >= wanted:
                break
        return frames

    everything = async_to_sync(collect)("0-0", 2)
    assert everything[0].startswith(f"id: {first}\nevent: run.started")
    assert '"n": 2' in everything[1]

    resumed = async_to_sync(collect)(first, 1)
    assert resumed[0].startswith(f"id: {second}\nevent: run.finished")


def test_idle_streams_send_heartbeats_and_stop_on_request() -> None:
    async def run() -> list[str]:
        frames: list[str] = []
        stop = {"flag": False}
        async for frame in sse.stream(
            f"idle-{uuid.uuid4()}", block_ms=50, should_stop=lambda: stop["flag"]
        ):
            frames.append(frame)
            if frame == sse.HEARTBEAT:
                stop["flag"] = True
        return frames

    frames = async_to_sync(run)()
    assert frames[0] == "retry: 3000\n\n" and frames[-1] == sse.HEARTBEAT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1700000000000-0", "1700000000000-0"),
        ("12", "12"),
        ("$", "$"),
        ("abc", "$"),
        ("1-2-3", "$"),
        (None, "$"),
    ],
)
def test_last_event_id_is_validated(raw: str | None, expected: str) -> None:
    assert sse.valid_last_event_id(raw) == expected


def test_stream_endpoint_requires_a_signed_in_member(workspace: Workspace, owner) -> None:
    anonymous = Client().get("/api/v1/events/stream", {"workspace_id": str(workspace.pk)})
    assert anonymous.status_code == 401

    member = Client()
    member.force_login(owner)
    assert member.get("/api/v1/events/stream", {"workspace_id": "nope"}).status_code == 422
    assert (
        member.get("/api/v1/events/stream", {"workspace_id": str(uuid.uuid4())}).status_code == 422
    )


def test_stream_endpoint_streams_published_events(workspace: Workspace, owner) -> None:
    client = AsyncClient()
    async_to_sync(client.aforce_login)(owner)
    sse.publish(sse.workspace_channel(workspace.pk), "hello", {"ok": True})

    async def first_frames() -> tuple[int, str, list[str]]:
        response = await client.get(
            "/api/v1/events/stream",
            {"workspace_id": str(workspace.pk)},
            headers={"Last-Event-ID": "0-0"},
        )
        frames: list[str] = []
        async for chunk in response.streaming_content:  # type: ignore[union-attr]
            frames.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
            if len(frames) == 2:
                break
        return response.status_code, response["Content-Type"], frames

    status, content_type, frames = async_to_sync(first_frames)()
    assert status == 200 and content_type == "text/event-stream"
    assert "event: hello" in frames[1]


# ---- partitioning -----------------------------------------------------------------------------


@pytest.fixture
def series_table():
    name = "test_series"
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {name}")
        cursor.execute(
            f"CREATE TABLE {name} (day date NOT NULL, value int) PARTITION BY RANGE (day)"
        )
    yield PartitionedTable(name, keep_months=3, months_ahead=2)
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {name}")


def test_partitions_are_created_idempotently_and_route_rows(series_table: PartitionedTable) -> None:
    today = date(2026, 11, 20)
    created = ensure_partitions(series_table, today=today)
    assert created == [
        partition_name("test_series", date(2026, 11, 1)),
        partition_name("test_series", date(2026, 12, 1)),
        partition_name("test_series", date(2027, 1, 1)),
    ]
    assert ensure_partitions(series_table, today=today) == []
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO test_series VALUES ('2026-12-05', 7)")
        cursor.execute("SELECT count(*) FROM test_series_p202612")
        assert cursor.fetchone() == (1,)


def test_year_boundaries_and_expiry(series_table: PartitionedTable) -> None:
    # 2026-06..08, 2026-11..2027-01 and 2027-02..04 exist after three maintenance runs.
    for today in (date(2026, 6, 1), date(2026, 11, 1), date(2027, 2, 1)):
        ensure_partitions(series_table, today=today)
    assert partition_name("test_series", date(2027, 1, 1)) == "test_series_p202701"

    # keep_months=3 on 2027-02-15 keeps everything from 2026-11-01 onwards.
    dropped = drop_expired_partitions(series_table, today=date(2027, 2, 15))
    assert dropped == ["test_series_p202606", "test_series_p202607", "test_series_p202608"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('test_series_p202611'), to_regclass('test_series_p202606')"
        )
        assert cursor.fetchone() == ("test_series_p202611", None)
    assert drop_expired_partitions(series_table, today=date(2027, 2, 15)) == []


def test_unsafe_table_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        PartitionedTable("metrics; DROP TABLE users")


def test_maintain_all_covers_registered_tables(series_table: PartitionedTable) -> None:
    register_partitioned_table(series_table)
    result = maintain_all(today=date(2026, 3, 10))
    assert result["test_series"]["created"][0] == "test_series_p202603"


# ---- drift watch ------------------------------------------------------------------------------


def test_sunset_findings_are_graded_by_urgency() -> None:
    versions = {"google_ads": "v22", "meta": "v26.0", "linkedin": "202604", "openai_ads": "v1"}
    sunsets = {
        "google_ads:v22": "2026-10-07",
        "meta:v26.0": "2028-07-29",
        "linkedin:202604": "2026-12-01",
    }
    findings = {
        f.provider: f
        for f in drift.check_version_sunsets(versions, sunsets, today=date(2026, 9, 29))
    }
    assert findings["google_ads"].level == "critical" and findings["google_ads"].days_left == 8
    assert findings["linkedin"].level == "warning"
    assert "meta" not in findings and "openai_ads" not in findings
    expired = drift.check_version_sunsets(versions, sunsets, today=date(2026, 10, 8))
    assert {f.level for f in expired if f.provider == "google_ads"} == {"expired"}


def test_document_normalisation_ignores_markup_and_scripts() -> None:
    a = "<html><script>var t=1</script><body><p>Rate limit:   600/min</p></body></html>"
    b = "<html><script>var t=2</script><body><div>Rate   limit: 600/min</div></body></html>"
    assert drift.normalise_document(a) == drift.normalise_document(b) == "Rate limit: 600/min"


def test_documentation_changes_are_detected_once() -> None:
    pages = {"text": "<p>version one</p>"}

    def fetch(url: str) -> httpx.Response:
        return httpx.Response(200, text=pages["text"], request=httpx.Request("GET", url))

    urls = {"vendor_docs": "https://docs.example.test/api"}
    assert drift.watch_docs(urls, fetch=fetch) == []  # first sight only records a baseline
    assert drift.watch_docs(urls, fetch=fetch) == []
    pages["text"] = "<p>version two</p>"
    assert drift.watch_docs(urls, fetch=fetch) == ["vendor_docs"]
    assert drift.watch_docs(urls, fetch=fetch) == []
    snapshot = DocSnapshot.objects.get(name="vendor_docs")
    assert snapshot.changed_at is not None and snapshot.previous_hash != snapshot.content_hash
    assert AuditEvent.objects.filter(action="platform.docs_changed").exists()
    assert OutboxMessage.objects.filter(topic="platform.drift").exists()


def test_failed_fetches_do_not_break_the_sweep() -> None:
    def fetch(url: str) -> httpx.Response:
        if "bad" in url:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

    urls = {"good": "https://good.example.test/", "bad": "https://bad.example.test/"}
    assert drift.watch_docs(urls, fetch=fetch) == []
    assert (
        DocSnapshot.objects.filter(name="good").exists()
        and not DocSnapshot.objects.filter(name="bad").exists()
    )


@override_settings(
    PROVIDER_API_VERSIONS={"google_ads": "v22"},
    PROVIDER_SUNSET_DATES={"google_ads:v22": "2026-10-07"},
    PROVIDER_DOC_URLS={},
)
def test_drift_watch_task_publishes_events_for_expiring_versions() -> None:
    result = drift.run_drift_watch(today=date(2026, 9, 29))
    assert result["sunsets"] == ["critical"]
    assert OutboxMessage.objects.filter(topic="platform.drift", payload__level="critical").exists()
