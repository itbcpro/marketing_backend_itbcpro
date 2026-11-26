"""The routine: gather trends, plan ahead, start writing. Runs daily for every brand with automation
on, and on demand when the owner asks. A run row exists from the moment work is requested, so the
screen can follow it."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.errors import DomainError
from core.logging import get_logger
from studio import clock, config_store, planner, strategist
from studio.models import Brand, RunKind, RunStatus, StudioProfile, StudioRun
from studio.trends import collect

log = get_logger(__name__)

STALE_RUN = timedelta(hours=1)


def due_brands(now: datetime | None = None) -> list[uuid.UUID]:
    """Brands with automation on whose daily hour has come and that have not run today."""
    moment = now or timezone.now()
    due: list[uuid.UUID] = []
    profiles = StudioProfile.objects.select_related("brand").filter(brand__archived_at__isnull=True)
    for profile in profiles:
        config = config_store.load(profile.brand_id)
        if not config.automation.enabled or not config.enabled_channels():
            continue
        local = clock.local_now(profile.workspace_id, moment)
        if local.hour < config.automation.run_hour:
            continue
        already = (
            StudioRun.objects.filter(
                brand_id=profile.brand_id, kind=RunKind.DAILY, local_date=local.date()
            )
            .filter(
                Q(status=RunStatus.DONE)
                | Q(status=RunStatus.RUNNING, started_at__gte=moment - STALE_RUN)
            )
            .exists()
        )
        if not already:
            due.append(profile.brand_id)
    return due


def start(brand: Brand, kind: str, *, now: datetime | None = None, **inputs: Any) -> StudioRun:
    """Record a run and queue its work."""
    moment = now or timezone.now()
    run = StudioRun.objects.create(
        workspace_id=brand.workspace_id,
        brand=brand,
        kind=kind,
        local_date=clock.local_now(brand.workspace_id, moment).date(),
        stats={"inputs": inputs},
    )
    from studio.tasks import run_task

    transaction.on_commit(lambda: run_task.delay(str(run.pk)))
    return run


def execute(run_id: uuid.UUID, now: datetime | None = None) -> StudioRun | None:
    """Do the work of a queued run and record the outcome."""
    run = (
        StudioRun.objects.select_related("brand")
        .filter(pk=run_id, status=RunStatus.RUNNING)
        .first()
    )
    if run is None:
        return None
    try:
        stats = _ideas(run) if run.kind == RunKind.IDEAS else _routine(run, now or timezone.now())
    except DomainError as exc:
        return _finish(run, RunStatus.FAILED, {}, exc.code.value)
    except Exception:
        log.exception("studio_run_crashed")
        return _finish(run, RunStatus.FAILED, {}, "unknown")
    return _finish(run, RunStatus.DONE, stats, "")


def _ideas(run: StudioRun) -> dict[str, Any]:
    inputs = run.stats.get("inputs", {})
    made = strategist.generate(
        run.brand, int(inputs.get("count") or 5), str(inputs.get("instruction") or "")
    )
    return {"ideas_made": len(made)}


def _routine(run: StudioRun, moment: datetime) -> dict[str, Any]:
    gathered = collect.collect_brand(run.brand_id)
    filled = planner.fill_calendar(run.brand, now=moment)
    return {
        "sources": gathered.sources,
        "sources_failed": gathered.failed,
        "new_signals": gathered.new_items,
        "slots": filled.slots,
        "pieces_started": filled.created,
        "unfilled": filled.unfilled,
        "ideas_made": filled.ideas_made,
        "idea_error": filled.idea_error,
    }


def _finish(run: StudioRun, status: str, stats: dict[str, Any], error: str) -> StudioRun:
    run.status = status
    run.stats = stats
    run.error_code = error
    run.finished_at = timezone.now()
    run.save()
    return run
