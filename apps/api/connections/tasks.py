"""Scheduled connection jobs."""

from typing import Any

from connections import drift, lifecycle
from core.tasks import ReliableTask, reliable_task


@reliable_task("connections.refresh_tokens", queue="default", time_limit=300, soft_time_limit=270)
def refresh_tokens(self: ReliableTask) -> int:
    return lifecycle.refresh_due_connections()


@reliable_task("connections.drift_watch", queue="low", time_limit=300, soft_time_limit=270)
def drift_watch(self: ReliableTask) -> dict[str, Any]:
    return drift.run_drift_watch()


@reliable_task("connections.check_health", queue="low", time_limit=900, soft_time_limit=840)
def check_health(self: ReliableTask) -> dict[str, Any]:
    return lifecycle.check_all_connections()
