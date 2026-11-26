"""Advertising background jobs."""

import uuid

from core.locks import LockNotAcquiredError, redis_lock
from core.tasks import ReliableTask, reliable_task


@reliable_task("agents.assist_turn", queue="agents", time_limit=600, soft_time_limit=540)
def assist_turn_task(self: ReliableTask, session_id: str) -> str:
    """Draft the assistant's next message. A per-conversation lock prevents double runs."""
    from ads.openai import sessions

    try:
        with redis_lock(f"assist-turn:{session_id}", ttl_seconds=660):
            return sessions.run_turn(uuid.UUID(session_id))
    except LockNotAcquiredError:
        return "already_running"


@reliable_task("agents.ad_review", queue="agents", time_limit=600, soft_time_limit=540)
def ad_review_task(self: ReliableTask, review_id: str) -> str:
    """Run one AI review. A per-review lock prevents double runs."""
    from ads.openai import review

    try:
        with redis_lock(f"ad-review:{review_id}", ttl_seconds=660):
            return review.run_review(uuid.UUID(review_id))
    except LockNotAcquiredError:
        return "already_running"


@reliable_task("ads.sync_accounts", queue="low", time_limit=1800, soft_time_limit=1740)
def sync_accounts_task(self: ReliableTask) -> dict[str, int]:
    """Daily refresh of every active ad account, then the alert checks."""
    from ads import alerts, providers
    from connections.models import Connection, ConnectionStatus
    from core.errors import DomainError

    done = 0
    for connection in Connection.objects.filter(
        provider__in=list(providers.PROVIDERS), status=ConnectionStatus.ACTIVE
    ):
        try:
            providers.for_connection(connection).sync(connection)
            done += 1
        except DomainError:
            continue  # the error is recorded on the sync state; other accounts still sync
    alerts.evaluate_all()
    return {"synced": done}


@reliable_task("agents.ad_variants", queue="agents", time_limit=300, soft_time_limit=270)
def ad_variants_task(self: ReliableTask, set_id: str) -> str:
    """Write three alternative versions of an ad. A per-set lock prevents double runs."""
    from ads.openai import variants

    try:
        with redis_lock(f"ad-variants:{set_id}", ttl_seconds=330):
            return variants.run(uuid.UUID(set_id))
    except LockNotAcquiredError:
        return "already_running"


@reliable_task("agents.ad_hints", queue="agents", time_limit=300, soft_time_limit=270)
def ad_hints_task(self: ReliableTask, proposal_id: str) -> str:
    """Clean up an ad group's context hints. A per-proposal lock prevents double runs."""
    from ads.openai import hints

    try:
        with redis_lock(f"ad-hints:{proposal_id}", ttl_seconds=330):
            return hints.run(uuid.UUID(proposal_id))
    except LockNotAcquiredError:
        return "already_running"


@reliable_task("agents.ad_keywords", queue="agents", time_limit=300, soft_time_limit=270)
def ad_keywords_task(self: ReliableTask, proposal_id: str) -> str:
    """Suggest keyword changes from an ad group's search terms. One run at a time per proposal."""
    from ads.google import keywords

    try:
        with redis_lock(f"ad-keywords:{proposal_id}", ttl_seconds=330):
            return keywords.run(uuid.UUID(proposal_id))
    except LockNotAcquiredError:
        return "already_running"


@reliable_task("agents.ad_optimize", queue="agents", time_limit=900, soft_time_limit=840)
def ad_optimize_task(self: ReliableTask, run_id: str) -> str:
    """Audit and optimise one Google Ads campaign. One run at a time per run id."""
    from ads.google import optimize

    try:
        with redis_lock(f"ad-optimize:{run_id}", ttl_seconds=960):
            return optimize.run(uuid.UUID(run_id))
    except LockNotAcquiredError:
        return "already_running"


@reliable_task("agents.openai_launch", queue="agents", time_limit=900, soft_time_limit=840)
def launch_task(self: ReliableTask, session_id: str, user_id: str) -> str:
    """Create the approved draft step by step. A per-conversation lock prevents double runs."""
    from ads.openai import launcher

    try:
        with redis_lock(f"openai-launch:{session_id}", ttl_seconds=960):
            return launcher.run(uuid.UUID(session_id), uuid.UUID(user_id))
    except LockNotAcquiredError:
        return "already_running"
