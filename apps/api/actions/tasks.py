"""Action execution and housekeeping jobs."""

import uuid

from actions import services
from actions.registry import UnknownActionKindError
from core.errors import DomainError, ErrorCode, RetryableProviderError
from core.tasks import ReliableTask, reliable_task

UNKNOWN_KIND_RETRY_SECONDS = 30
UNKNOWN_KIND_RETRIES = 20


@reliable_task("actions.execute", queue="default", time_limit=600, soft_time_limit=540)
def execute_action_task(self: ReliableTask, action_id: str) -> str:
    """Execute one approved action. Retries transient provider failures, then gives up."""
    try:
        return str(services.execute(uuid.UUID(action_id)).status)
    except RetryableProviderError as exc:
        if self.request.retries >= self.max_retries:
            services.fail_final(uuid.UUID(action_id), str(exc))
            return "failed_final"
        raise
    except UnknownActionKindError as exc:
        # This worker was started before the action kind existed (old code). Waiting lets a
        # restarted worker pick the action up instead of losing it to the dead-letter table.
        raise self.retry(
            exc=exc, countdown=UNKNOWN_KIND_RETRY_SECONDS, max_retries=UNKNOWN_KIND_RETRIES
        ) from exc
    except DomainError as exc:
        if exc.code == ErrorCode.KILL_SWITCH:
            return "paused"  # stays approved; re-enqueued when the switch is released
        raise


@reliable_task("actions.expire_stale", queue="low")
def expire_stale(self: ReliableTask) -> int:
    return services.expire_stale()


@reliable_task("actions.recover_stuck", queue="low")
def recover_stuck(self: ReliableTask) -> int:
    recovered = services.recover_stuck()
    services.redispatch_approved()
    return recovered
