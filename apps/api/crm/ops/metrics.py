"""CRM metrics for Prometheus: counters where things happen, gauges read at scrape time.

Labels are small closed sets (channel, outcome, source, step type). Nothing that names a person,
a message or a workspace is ever a label.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import Counter

from core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from core.metrics import REGISTRY

MESSAGES = Counter(
    "crm_messages_total",
    "Outbound messages by final outcome",
    ["channel", "outcome"],
    registry=REGISTRY,
)
WEBHOOKS = Counter(
    "crm_webhooks_total",
    "Public callbacks by source and outcome",
    ["source", "outcome"],
    registry=REGISTRY,
)
WORKFLOW_STEPS = Counter(
    "crm_workflow_steps_total",
    "Automation steps by type and outcome",
    ["step_type", "outcome"],
    registry=REGISTRY,
)


def message_outcome(channel: str, outcome: str) -> None:
    MESSAGES.labels(channel=channel or "unknown", outcome=outcome).inc()


def workflow_step(step_type: str, outcome: str) -> None:
    WORKFLOW_STEPS.labels(step_type=step_type, outcome=outcome).inc()


@contextmanager
def watch(source: str) -> Iterator[None]:
    """Count one public callback: accepted, refused for its signature, unknown or malformed."""
    try:
        yield
    except PermissionDeniedError:
        WEBHOOKS.labels(source=source, outcome="bad_signature").inc()
        raise
    except NotFoundError:
        WEBHOOKS.labels(source=source, outcome="not_found").inc()
        raise
    except ValidationFailedError:
        WEBHOOKS.labels(source=source, outcome="invalid").inc()
        raise
    WEBHOOKS.labels(source=source, outcome="ok").inc()
