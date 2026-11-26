"""Scheduled memory jobs."""

from core.tasks import ReliableTask, reliable_task
from memory import reflection


@reliable_task("memory.weekly_reflection", queue="agents", time_limit=1800, soft_time_limit=1700)
def weekly_reflection(self: ReliableTask) -> dict[str, int]:
    return reflection.reflect_all_workspaces()
