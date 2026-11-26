"""Reliable Celery task base: bounded retries with jitter, dead letters, correlation ids."""

import traceback
import uuid
from typing import Any

import celery
from celery import shared_task
from celery.signals import before_task_publish, task_failure, task_postrun, task_prerun
from django.db import OperationalError
from structlog.contextvars import bind_contextvars, clear_contextvars, get_contextvars

from core import tenancy
from core.errors import RetryableProviderError
from core.logging import get_logger, scrub_mapping
from core.metrics import DEAD_LETTERS, TASKS

log = get_logger(__name__)

RETRY_BASE_SECONDS = 2
RETRY_MAX_SECONDS = 900
MAX_RETRIES = 8


class ReliableTask(celery.Task):  # type: ignore[misc]
    """Retries transient failures with exponential backoff and jitter; records the rest."""

    abstract = True
    autoretry_for = (RetryableProviderError, OperationalError, ConnectionError, TimeoutError)
    retry_backoff = RETRY_BASE_SECONDS
    retry_backoff_max = RETRY_MAX_SECONDS
    retry_jitter = True
    max_retries = MAX_RETRIES
    acks_late = True
    reject_on_worker_lost = True

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: Any,
    ) -> None:
        from core.models import DeadLetter

        DEAD_LETTERS.labels(task=self.name).inc()
        log.error("task_dead_lettered", task=self.name, task_id=task_id, error=type(exc).__name__)
        try:
            DeadLetter.objects.create(
                task_name=self.name or "",
                task_id=task_id,
                args=scrub_mapping(list(args)),
                kwargs=scrub_mapping(kwargs),
                error=f"{type(exc).__name__}: {exc}"[:2000],
                traceback="".join(traceback.format_exception(exc))[-8000:],
            )
        except Exception:
            log.exception("dead_letter_write_failed", task=self.name)


def reliable_task(name: str, *, queue: str = "default", **options: Any) -> Any:
    """Declare a task with the reliable defaults."""
    return shared_task(base=ReliableTask, bind=True, name=name, queue=queue, **options)


class TenantTask(ReliableTask):
    """A reliable task that runs inside one workspace. ``workspace_id`` is a required keyword."""

    abstract = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raw = kwargs.get("workspace_id")
        if raw is None:
            raise ValueError(f"{self.name} requires the keyword argument workspace_id")
        with tenancy.use_workspace(uuid.UUID(str(raw))):
            return super().__call__(*args, **kwargs)


def tenant_task(name: str, *, queue: str = "default", **options: Any) -> Any:
    """Declare a task that always runs in the tenant context of its ``workspace_id``."""
    return shared_task(base=TenantTask, bind=True, name=name, queue=queue, **options)


@reliable_task("core.housekeeping", queue="low")
def housekeeping(self: ReliableTask) -> dict[str, int]:
    from core.housekeeping import run_housekeeping

    return run_housekeeping()


@reliable_task("core.maintain_partitions", queue="low")
def maintain_partitions(self: ReliableTask) -> dict[str, Any]:
    from core.partitioning import maintain_all

    return maintain_all()


@before_task_publish.connect
def _propagate_request_id(headers: dict[str, Any] | None = None, **_: Any) -> None:
    request_id = get_contextvars().get("request_id")
    if request_id and headers is not None:
        headers["request_id"] = request_id


@task_prerun.connect
def _bind_task_context(task_id: str | None = None, task: Any = None, **_: Any) -> None:
    clear_contextvars()
    bind_contextvars(
        task_id=task_id,
        task=getattr(task, "name", ""),
        request_id=getattr(getattr(task, "request", None), "request_id", None) or task_id,
    )


@task_postrun.connect
def _count_outcome(task: Any = None, state: str | None = None, **_: Any) -> None:
    TASKS.labels(task=getattr(task, "name", "unknown"), state=state or "unknown").inc()


@task_failure.connect
def _count_failure(sender: Any = None, **_: Any) -> None:
    TASKS.labels(task=getattr(sender, "name", "unknown"), state="FAILURE_FINAL").inc()
