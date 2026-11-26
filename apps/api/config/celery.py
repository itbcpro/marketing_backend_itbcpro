"""Celery application with per-provider queues and reliable defaults.

DJANGO_SETTINGS_MODULE must be provided by the environment; this module never picks a default,
because it is imported before any entry point and would silently select development settings.
"""

from celery import Celery
from kombu import Exchange, Queue

QUEUE_NAMES = (
    "default",
    "agents",
    "ingest.meta",
    "ingest.google",
    "ingest.openai",
    "ingest.social",
    "publish.meta",
    "publish.google",
    "publish.openai",
    "publish.social",
    "media",
    "reports",
    "low",
)

app = Celery("marketing")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.task_queues = tuple(Queue(name, Exchange(name), routing_key=name) for name in QUEUE_NAMES)
app.conf.task_routes = {
    "audit.dispatch_outbox": {"queue": "default"},
    "agents.*": {"queue": "agents"},
    "reporting.*": {"queue": "reports"},
    "media.*": {"queue": "media"},
}
app.conf.beat_schedule = {
    "dispatch-outbox": {"task": "audit.dispatch_outbox", "schedule": 5.0},
    "verify-audit-chain": {"task": "audit.verify_chain", "schedule": 3600.0},
    "connections-refresh-tokens": {"task": "connections.refresh_tokens", "schedule": 600.0},
    "connections-check-health": {"task": "connections.check_health", "schedule": 3600.0},
    "ads-sync-accounts": {"task": "ads.sync_accounts", "schedule": 86400.0},
    "crm-bridge-instagram": {"task": "crm.inbox.bridge_instagram_all", "schedule": 60.0},
    "crm-recheck-domains": {"task": "crm.sending.recheck_domains_all", "schedule": 3600.0},
    "crm-campaign-tick": {"task": "crm.campaigns.tick", "schedule": 30.0},
    "crm-notification-digests": {"task": "crm.notifications.send_digests", "schedule": 3600.0},
    "crm-meta-sync-templates": {"task": "crm.meta.sync_templates_all", "schedule": 3600.0},
    "crm-workflows-wake": {"task": "crm.workflows.wake", "schedule": 30.0},
    "crm-privacy-retention": {"task": "crm.privacy.run_retention", "schedule": 86400.0},
    "crm-billing-invoices": {"task": "crm.billing.generate_invoices", "schedule": 86400.0},
    "social-sync-all": {"task": "social.sync_all", "schedule": 21600.0},
    "social-inbox-cycle": {"task": "social.inbox_cycle_all", "schedule": 600.0},
    "social-dispatch-auto": {"task": "social.dispatch_auto", "schedule": 60.0},
    "social-publish-due": {"task": "social.publish_due", "schedule": 60.0},
    "social-refresh-tokens": {"task": "social.refresh_tokens", "schedule": 86400.0},
    "studio-daily-tick": {"task": "studio.daily_tick", "schedule": 900.0},
    "studio-dispatch-due": {"task": "studio.dispatch_due", "schedule": 60.0},
    "studio-collect-trends": {"task": "studio.collect_all", "schedule": 10800.0},
    "connections-drift-watch": {"task": "connections.drift_watch", "schedule": 86400.0},
    "core-maintain-partitions": {"task": "core.maintain_partitions", "schedule": 86400.0},
    "actions-expire-stale": {"task": "actions.expire_stale", "schedule": 900.0},
    "actions-recover-stuck": {"task": "actions.recover_stuck", "schedule": 300.0},
    "memory-weekly-reflection": {"task": "memory.weekly_reflection", "schedule": 604800.0},
    "audit-retention": {"task": "audit.run_retention", "schedule": 86400.0},
    "content-probe-websites": {"task": "content.probe_all", "schedule": 21600.0},
    "content-sync-websites": {"task": "content.sync_all", "schedule": 21600.0},
    "content-search-sync": {"task": "content.search_sync_all", "schedule": 43200.0},
    "core-housekeeping": {"task": "core.housekeeping", "schedule": 86400.0},
}
app.autodiscover_tasks()
