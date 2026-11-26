"""Background jobs of the content studio."""

import uuid

from core.locks import LockNotAcquiredError, redis_lock
from core.tasks import ReliableTask, reliable_task


@reliable_task("studio.write_piece", queue="agents", time_limit=900, soft_time_limit=840)
def write_piece_task(self: ReliableTask, piece_id: str) -> str:
    """Write one piece and make its pictures. A per-piece lock prevents double runs."""
    from studio import writer

    try:
        with redis_lock(f"studio-write:{piece_id}", ttl_seconds=960):
            return writer.run(uuid.UUID(piece_id))
    except LockNotAcquiredError:
        return "already_running"


@reliable_task("studio.run", queue="agents", time_limit=1200, soft_time_limit=1140)
def run_task(self: ReliableTask, run_id: str) -> str:
    """Do the work of one recorded run (daily routine, a run the owner asked for, or ideas)."""
    from studio import runs
    from studio.models import StudioRun

    run = StudioRun.objects.filter(pk=run_id).first()
    if run is None:
        return "missing"
    try:
        with redis_lock(f"studio-run:{run.brand_id}", ttl_seconds=1260):
            outcome = runs.execute(uuid.UUID(run_id))
            return outcome.status if outcome else "skipped"
    except LockNotAcquiredError:
        return "already_running"


@reliable_task("studio.daily_tick", queue="low", time_limit=120, soft_time_limit=100)
def daily_tick_task(self: ReliableTask) -> int:
    """Start the routine for every brand whose hour has come (cheap when idle)."""
    from studio import runs
    from studio.models import Brand

    due = runs.due_brands()
    for brand in Brand.objects.filter(pk__in=due):
        runs.start(brand, "daily")
    return len(due)


@reliable_task("studio.collect_trends", queue="ingest.social", time_limit=600, soft_time_limit=540)
def collect_trends_task(self: ReliableTask, brand_id: str) -> int:
    """Refresh the trend sources of one brand."""
    from studio.trends import collect

    return collect.collect_brand(uuid.UUID(brand_id)).new_items


@reliable_task("studio.collect_all", queue="low", time_limit=300, soft_time_limit=270)
def collect_all_task(self: ReliableTask) -> int:
    """Queue a trend refresh for every brand that has sources turned on."""
    from studio.models import TrendSource

    ids = TrendSource.objects.filter(enabled=True).values_list("brand_id", flat=True).distinct()
    queued = 0
    for brand_id in ids:
        collect_trends_task.delay(str(brand_id))
        queued += 1
    return queued


@reliable_task("studio.dispatch_due", queue="publish.social", time_limit=120, soft_time_limit=100)
def dispatch_due_task(self: ReliableTask) -> dict[str, int]:
    """Mark hand-posted pieces as due, and copy the state of handed-over ones."""
    from studio import services

    return {**services.dispatch_due(), **services.sync_external()}
