from datetime import UTC, datetime, timedelta

import pytest

from core.errors import RetryableProviderError
from llm.fake import FakeGateway
from llm.service import LlmService, set_llm_service
from studio import config_store, net, runs
from studio.models import (
    ContentIdea,
    ContentPiece,
    RunKind,
    RunStatus,
    StudioRun,
    TrendItem,
    TrendSource,
)
from studio.tests.conftest import brand_of, feed_transport, ideas, llm, make_config

pytestmark = pytest.mark.django_db

MONDAY = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
FEED = b"""<rss version="2.0"><channel><item><title>Printers jam again</title>
<link>https://n.example.com/1</link></item></channel></rss>"""


@pytest.fixture
def daily(workspace, owner):
    config = make_config(automation={"enabled": True, "run_hour": 6, "horizon_days": 6})
    config_store.save(brand_of(workspace), config, owner)
    return config


def test_the_routine_is_due_once_its_hour_has_come(workspace, daily):
    early = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    assert runs.due_brands(early) == []
    assert runs.due_brands(MONDAY) == [brand_of(workspace).pk]


def test_the_hour_is_read_in_the_workspace_time_zone(workspace, daily):
    workspace.time_zone = "America/Toronto"
    workspace.save()
    assert runs.due_brands(datetime(2026, 10, 12, 8, 0, tzinfo=UTC)) == []  # 04:00 there
    assert runs.due_brands(datetime(2026, 10, 12, 11, 0, tzinfo=UTC)) == [brand_of(workspace).pk]


def test_a_finished_run_today_means_not_due_but_tomorrow_it_is(workspace, daily):
    run = runs.start(brand_of(workspace), RunKind.DAILY, now=MONDAY)
    StudioRun.objects.filter(pk=run.pk).update(status=RunStatus.DONE)
    assert runs.due_brands(MONDAY + timedelta(hours=3)) == []
    assert runs.due_brands(MONDAY + timedelta(days=1)) == [brand_of(workspace).pk]


def test_a_run_that_hangs_does_not_block_the_day_forever(workspace, daily):
    run = StudioRun.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind=RunKind.DAILY,
        local_date=MONDAY.date(),
    )
    StudioRun.objects.filter(pk=run.pk).update(started_at=MONDAY)
    assert runs.due_brands(MONDAY + timedelta(minutes=30)) == []
    assert runs.due_brands(MONDAY + timedelta(hours=3)) == [brand_of(workspace).pk]


def test_nothing_is_due_without_automation_or_channels(workspace, owner):
    config_store.save(brand_of(workspace), make_config(), owner)
    assert runs.due_brands(MONDAY) == []
    config_store.save(
        brand_of(workspace), make_config(automation={"enabled": True}, channels={}), owner
    )
    assert runs.due_brands(MONDAY) == []


def test_the_routine_gathers_trends_then_plans_and_starts_writing(workspace, daily, monkeypatch):
    monkeypatch.setattr(net, "transport", feed_transport({"https://feeds.example.com/": FEED}))
    TrendSource.objects.create(
        workspace_id=workspace.pk,
        brand=brand_of(workspace),
        kind="rss",
        name="Blog",
        params={"url": "https://feeds.example.com/a.xml"},
    )
    llm(ideas(5))
    run = runs.start(brand_of(workspace), RunKind.DAILY, now=MONDAY)
    done = runs.execute(run.pk, now=MONDAY)
    assert done.status == RunStatus.DONE and done.finished_at
    assert (
        done.stats["new_signals"] == 1 and done.stats["pieces_started"] == done.stats["slots"] > 0
    )
    assert TrendItem.objects.count() == 1 and ContentPiece.objects.count() == done.stats["slots"]


def test_a_run_is_executed_only_once(workspace, daily):
    llm(ideas(5))
    run = runs.start(brand_of(workspace), RunKind.MANUAL, now=MONDAY)
    runs.execute(run.pk, now=MONDAY)
    assert runs.execute(run.pk, now=MONDAY) is None


def test_asking_for_ideas_records_how_many_came_back(workspace, daily):
    llm(ideas(3))
    run = runs.start(brand_of(workspace), RunKind.IDEAS, count=3, instruction="Focus on security")
    done = runs.execute(run.pk)
    assert done.stats == {"ideas_made": 3} and ContentIdea.objects.count() == 3


def test_a_failed_idea_request_shows_why(workspace, daily):
    set_llm_service(LlmService(FakeGateway([RetryableProviderError("down", provider="x")])))
    run = runs.start(brand_of(workspace), RunKind.IDEAS, count=3)
    done = runs.execute(run.pk)
    assert done.status == RunStatus.FAILED and done.error_code
