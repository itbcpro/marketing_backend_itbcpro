"""Bulk actions over a snapshot of contacts, run in chunks with per-record results."""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.contacts import services as contacts
from crm.lists.models import BulkAction, BulkJob, JobStatus
from workspaces.models import Membership

ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)


def _check_params(membership: Membership, action: str, params: dict[str, Any]) -> dict[str, Any]:
    if action in {BulkAction.ADD_TAGS, BulkAction.REMOVE_TAGS}:
        names = [str(n).strip() for n in params.get("names", []) if str(n).strip()]
        if not names:
            raise ValidationFailedError("tags required", errors={"names": "required"})
        return {"names": names}
    if action == BulkAction.ASSIGN:
        user = params.get("user_id")
        return {"user_id": str(user) if user else None}
    return {}


def create(
    *,
    membership: Membership,
    actor: Actor,
    action: str,
    params: dict[str, Any],
    spec: dict[str, Any] | None = None,
    text: str | None = None,
    contact_ids: list[uuid.UUID] | None = None,
) -> BulkJob:
    from crm.lists.tasks import run_bulk_task

    if action not in BulkAction.values:
        raise ValidationFailedError("invalid action", errors={"action": "invalid_value"})
    clean = _check_params(membership, action, params)
    visible = contacts.query_contacts(membership, spec=spec, text=text)
    if contact_ids is not None:
        visible = visible.filter(pk__in=contact_ids)
    limit = settings.CRM_BULK_MAX_RECORDS
    ids = [str(pk) for pk in visible.values_list("pk", flat=True)[: limit + 1]]
    if len(ids) > limit:
        raise ValidationFailedError("too many records", errors={"target": "too_many"})
    if not ids:
        raise ValidationFailedError("nothing selected", errors={"target": "empty"})
    with transaction.atomic():
        job = BulkJob.objects.create(
            workspace_id=membership.workspace_id,
            action=action,
            params=clean,
            contact_ids=ids,
            total=len(ids),
            created_by_id=membership.user_id,
        )
        record_event(
            action="crm.bulk.created",
            actor=actor,
            workspace_id=membership.workspace_id,
            target_type="crm_bulk_job",
            target_id=job.pk,
            after={"action": action, "total": len(ids)},
        )
        transaction.on_commit(
            lambda: run_bulk_task.delay(workspace_id=str(job.workspace_id), job_id=str(job.pk))
        )
    return job


def get_job(workspace_id: uuid.UUID, job_id: uuid.UUID) -> BulkJob:
    found = BulkJob.objects.for_workspace(workspace_id).filter(pk=job_id).first()
    if found is None:
        raise NotFoundError("bulk job not found")
    return found


def cancel(*, workspace_id: uuid.UUID, job_id: uuid.UUID) -> BulkJob:
    with transaction.atomic():
        job = (
            BulkJob.objects.select_for_update().filter(workspace_id=workspace_id, pk=job_id).first()
        )
        if job is None:
            raise NotFoundError("bulk job not found")
        if job.status not in ACTIVE:
            raise ConflictError("job already finished", errors={"job": "already_finished"})
        job.cancel_requested = True
        if job.status == JobStatus.QUEUED:
            job.status, job.finished_at = JobStatus.CANCELED, timezone.now()
        job.save()
    return job


def _apply(job: BulkJob, actor: Actor, contact_id: uuid.UUID) -> None:
    workspace_id = job.workspace_id
    if job.action == BulkAction.DELETE:
        contacts.delete_contact(workspace_id=workspace_id, actor=actor, contact_id=contact_id)
    elif job.action == BulkAction.ASSIGN:
        user = job.params.get("user_id")
        contacts.assign(
            workspace_id=workspace_id,
            actor=actor,
            contact_id=contact_id,
            user_id=uuid.UUID(user) if user else None,
        )
    else:
        contact = contacts.get_contact(workspace_id, contact_id)
        change = contacts.add_tags if job.action == BulkAction.ADD_TAGS else contacts.remove_tags
        change(workspace_id=workspace_id, actor=actor, contact=contact, names=job.params["names"])


def run(*, workspace_id: uuid.UUID, job_id: uuid.UUID) -> BulkJob:
    """Process in chunks, checking for cancellation between chunks. Safe to call again."""
    job = get_job(workspace_id, job_id)
    if job.status not in ACTIVE:
        return job
    job.status = JobStatus.RUNNING
    job.save(update_fields=["status", "updated_at"])
    actor = Actor.human(job.created_by_id) if job.created_by_id else Actor.system("bulk")
    size = settings.CRM_BULK_CHUNK_SIZE
    while job.processed < job.total:
        fresh = BulkJob.objects.values_list("cancel_requested", flat=True).get(pk=job.pk)
        if fresh:
            job.status, job.finished_at = JobStatus.CANCELED, timezone.now()
            job.save()
            return job
        for raw in job.contact_ids[job.processed : job.processed + size]:
            try:
                with transaction.atomic():
                    _apply(job, actor, uuid.UUID(raw))
                job.succeeded += 1
            except (NotFoundError, ValidationFailedError, ConflictError) as exc:
                job.failed += 1
                if len(job.failures) < settings.CRM_IMPORT_ERROR_LIMIT:
                    job.failures.append({"contact_id": raw, "error": exc.code.value})
            job.processed += 1
        job.save()
    job.status, job.finished_at = JobStatus.DONE, timezone.now()
    job.save()
    return job
