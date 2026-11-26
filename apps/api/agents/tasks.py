"""Agent execution jobs."""

import uuid

from agents import runtime
from core.locks import LockNotAcquiredError, redis_lock
from core.tasks import ReliableTask, reliable_task


@reliable_task("agents.execute_run", queue="agents", time_limit=1200, soft_time_limit=1140)
def execute_run_task(self: ReliableTask, run_id: str) -> str:
    """Run one agent to completion. A per-run lock prevents two workers from running it twice."""
    try:
        with redis_lock(f"agent-run:{run_id}", ttl_seconds=1300):
            return str(runtime.run_agent(uuid.UUID(run_id)).status)
    except LockNotAcquiredError:
        return "already_running"
