"""Background jobs of the social domain."""

import uuid
from typing import Any

from core.locks import LockNotAcquiredError, redis_lock
from core.tasks import ReliableTask, reliable_task


@reliable_task("social.sync_account", queue="ingest.social", time_limit=900, soft_time_limit=840)
def sync_account_task(
    self: ReliableTask, connection_id: str, inbox_only: bool = False
) -> dict[str, int]:
    """Refresh one Instagram account. A per-connection lock prevents overlapping syncs."""
    from connections.models import Connection
    from social import sync

    connection = Connection.objects.filter(pk=connection_id).first()
    if connection is None:
        return {}
    try:
        with redis_lock(f"social-sync:{connection_id}", ttl_seconds=960):
            return sync.sync_connection(connection, inbox_only=inbox_only)
    except LockNotAcquiredError:
        return {}


@reliable_task("social.sync_all", queue="low", time_limit=300, soft_time_limit=270)
def sync_all_task(self: ReliableTask, inbox_only: bool = False) -> int:
    """Queue a sync for every active Instagram connection (daily full, every few minutes inbox)."""
    from connections.models import Connection, ConnectionStatus
    from social.instagram.client import PROVIDER

    queued = 0
    for connection in Connection.objects.filter(provider=PROVIDER, status=ConnectionStatus.ACTIVE):
        sync_account_task.delay(str(connection.pk), inbox_only)
        queued += 1
    return queued


@reliable_task("social.inbox_cycle", queue="ingest.social", time_limit=900, soft_time_limit=840)
def inbox_cycle_task(self: ReliableTask, account_id: str) -> dict[str, Any]:
    """Pull new comments and messages, then let the AI triage and answer within the settings."""
    from social import messaging, replies, sync
    from social.models import SocialAccount

    account = SocialAccount.objects.select_related("connection").filter(pk=account_id).first()
    if account is None:
        return {}
    try:
        with redis_lock(f"social-inbox:{account_id}", ttl_seconds=960):
            counts: dict[str, Any] = dict(sync.sync_connection(account.connection, inbox_only=True))
            counts["comments_ai"] = replies.process_account(account)
            counts["messages_ai"] = messaging.process_account(account)
            return counts
    except LockNotAcquiredError:
        return {}


@reliable_task("social.inbox_cycle_all", queue="low", time_limit=300, soft_time_limit=270)
def inbox_cycle_all_task(self: ReliableTask) -> int:
    from social.models import SocialAccount

    queued = 0
    for account in SocialAccount.objects.filter(connection__status="active"):
        inbox_cycle_task.delay(str(account.pk))
        queued += 1
    return queued


@reliable_task("social.dispatch_auto", queue="publish.social", time_limit=300, soft_time_limit=270)
def dispatch_auto_task(self: ReliableTask) -> int:
    """Send the automatic replies whose delay has passed (runs every minute, cheap when idle)."""
    from social import replies
    from social.models import SocialAccount

    sent = 0
    for account in SocialAccount.objects.filter(connection__status="active"):
        sent += replies.dispatch_auto(account)
    return sent


@reliable_task("social.publish_due", queue="publish.social", time_limit=300, soft_time_limit=270)
def publish_due_task(self: ReliableTask) -> dict[str, int]:
    """Launch approved posts whose time has come."""
    from social import drafts

    try:
        with redis_lock("social-publish-due", ttl_seconds=120):
            return drafts.publish_due()
    except LockNotAcquiredError:
        return {}


@reliable_task("social.refresh_tokens", queue="low", time_limit=300, soft_time_limit=270)
def refresh_tokens_task(self: ReliableTask) -> int:
    from social.instagram import auth

    return auth.refresh_due()


@reliable_task("agents.social_report", queue="agents", time_limit=600, soft_time_limit=540)
def social_report_task(self: ReliableTask, report_id: str) -> str:
    """Write one AI report. A per-report lock prevents double runs."""
    from social import analysis

    try:
        with redis_lock(f"social-report:{report_id}", ttl_seconds=660):
            return analysis.run(uuid.UUID(report_id))
    except LockNotAcquiredError:
        return "already_running"


@reliable_task("agents.social_compose", queue="agents", time_limit=600, soft_time_limit=540)
def compose_draft_task(self: ReliableTask, draft_id: str) -> str:
    """Write one post and draw its pictures. A per-draft lock prevents double runs."""
    from social import composer

    try:
        with redis_lock(f"social-compose:{draft_id}", ttl_seconds=660):
            return composer.run(uuid.UUID(draft_id))
    except LockNotAcquiredError:
        return "already_running"
