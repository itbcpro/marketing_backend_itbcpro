"""CSV import: upload, map, dry run, and an idempotent resumable run."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.fields import services as fields
from crm.lists import csvio
from crm.lists.models import ImportJob, JobStatus

STANDARD_TARGETS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "address",
    "city",
    "state",
    "postal_code",
    "country",
    "source",
    "contact_type",
    "time_zone",
    "date_of_birth",
    "tags",
)
CUSTOM_PREFIX = "custom."
IGNORE = "ignore"
PREVIEW_ROWS = 5
TAG_SEPARATOR = ";"
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _key(header: str) -> str:
    return _NON_WORD.sub("_", header.strip().lower()).strip("_")


def _targets(workspace_id: uuid.UUID) -> set[str]:
    custom = {f"{CUSTOM_PREFIX}{d.key}" for d in fields.list_definitions(workspace_id, "contact")}
    return {*STANDARD_TARGETS, *custom, IGNORE}


def suggest_mapping(workspace_id: uuid.UUID, headers: list[str]) -> dict[str, str]:
    """Match headers to targets by normalised name; unknown headers are ignored."""
    targets = _targets(workspace_id)
    aliases = {
        "firstname": "first_name",
        "lastname": "last_name",
        "zip": "postal_code",
        "postal": "postal_code",
        "mobile": "phone",
        "tag": "tags",
    }
    out: dict[str, str] = {}
    for header in headers:
        key = _key(header)
        key = aliases.get(key.replace("_", ""), key)
        guess = key if key in targets else f"{CUSTOM_PREFIX}{key}"
        out[header] = guess if guess in targets else IGNORE
    return out


def create_import(
    *, workspace_id: uuid.UUID, actor: Actor, file_name: str, raw: bytes
) -> ImportJob:
    if len(raw) > settings.CRM_IMPORT_MAX_BYTES:
        raise ValidationFailedError("file too large", errors={"file": "too_large"})
    try:
        text = csvio.decode(raw)
    except UnicodeDecodeError as exc:
        raise ValidationFailedError("not utf-8", errors={"file": "invalid_encoding"}) from exc
    rows = csvio.read_rows(text)
    headers = next(rows, [])
    total = sum(1 for row in rows if any(cell.strip() for cell in row))
    cleaned = [h.strip() for h in headers]
    if not cleaned or not all(cleaned) or len(set(cleaned)) != len(cleaned):
        raise ValidationFailedError("bad header row", errors={"file": "invalid_header"})
    if total == 0 or total > settings.CRM_IMPORT_MAX_ROWS:
        raise ValidationFailedError("bad row count", errors={"file": "invalid_row_count"})
    job = ImportJob.objects.create(
        workspace_id=workspace_id,
        file_name=file_name[:200],
        content=text,
        headers=cleaned,
        mapping=suggest_mapping(workspace_id, cleaned),
        total_rows=total,
        created_by_id=uuid.UUID(actor.id) if actor.type == "human" else None,
    )
    record_event(
        action="crm.import.created",
        actor=actor,
        workspace_id=workspace_id,
        target_type="crm_import",
        target_id=job.pk,
        after={"rows": total},
    )
    return job


def get_job(workspace_id: uuid.UUID, job_id: uuid.UUID) -> ImportJob:
    found = ImportJob.objects.for_workspace(workspace_id).filter(pk=job_id).first()
    if found is None:
        raise NotFoundError("import not found")
    return found


def preview(job: ImportJob) -> list[dict[str, str]]:
    rows = csvio.read_rows(job.content)
    next(rows, None)
    out: list[dict[str, str]] = []
    for row in rows:
        if any(c.strip() for c in row):
            out.append(dict(zip(job.headers, row, strict=False)))
        if len(out) == PREVIEW_ROWS:
            break
    return out


def _check_mapping(workspace_id: uuid.UUID, headers: list[str], mapping: dict[str, str]) -> None:
    targets = _targets(workspace_id)
    errors: dict[str, str] = {}
    used: list[str] = []
    for header in headers:
        target = mapping.get(header, IGNORE)
        if target not in targets:
            errors[header] = "unknown_target"
        elif target != IGNORE:
            used.append(target)
    if len(used) != len(set(used)):
        errors["mapping"] = "duplicate_target"
    if not {"email", "phone"} & set(used):
        errors["mapping"] = "identity_required"
    if errors:
        raise ValidationFailedError("invalid mapping", errors=errors)


def _check_options(options: dict[str, Any]) -> dict[str, Any]:
    clean = {
        "tags": [str(t).strip() for t in options.get("tags", []) if str(t).strip()],
        "update_existing": bool(options.get("update_existing", True)),
        "consent": None,
    }
    wanted = options.get("consent")
    if wanted:
        if not wanted.get("attested"):
            raise ValidationFailedError(
                "attestation required", errors={"consent": "attestation_required"}
            )
        clean["consent"] = {
            "channel": wanted.get("channel"),
            "purpose": wanted.get("purpose", "marketing"),
        }
    return clean


def _row_data(headers: list[str], mapping: dict[str, str], row: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    custom: dict[str, Any] = {}
    for header, cell in zip(headers, row, strict=False):
        target, value = mapping.get(header, IGNORE), cell.strip()
        if target == IGNORE or not value:
            continue
        if target == "tags":
            data["tags"] = [t.strip() for t in value.split(TAG_SEPARATOR) if t.strip()]
        elif target.startswith(CUSTOM_PREFIX):
            custom[target.removeprefix(CUSTOM_PREFIX)] = value
        else:
            data[target] = value
    if custom:
        data["custom"] = custom
    return data


def dry_run(*, workspace_id: uuid.UUID, job: ImportJob, mapping: dict[str, str]) -> dict[str, Any]:
    """Validate every row without writing anything."""
    _check_mapping(workspace_id, job.headers, mapping)
    rows = csvio.read_rows(job.content)
    next(rows, None)
    seen: set[str] = set()
    valid = 0
    problems: list[dict[str, Any]] = []
    for number, row in enumerate(rows, start=2):
        if not any(c.strip() for c in row):
            continue
        data = _row_data(job.headers, mapping, row)
        try:
            clean = contacts.validate_contact_data(
                workspace_id, {k: v for k, v in data.items() if k not in {"custom", "tags"}}
            )
            identity = clean.get("email") or clean.get("phone") or ""
            if identity and identity in seen:
                raise ValidationFailedError(
                    "duplicate in file", errors={"row": "duplicate_in_file"}
                )
            seen.add(identity)
            valid += 1
        except ValidationFailedError as exc:
            if len(problems) < settings.CRM_IMPORT_ERROR_LIMIT:
                problems.append({"row": number, "errors": exc.params.get("errors", {})})
    return {"valid": valid, "invalid": job.total_rows - valid, "problems": problems}


def start(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    job_id: uuid.UUID,
    mapping: dict[str, str],
    options: dict[str, Any],
) -> ImportJob:
    from crm.lists.tasks import run_import_task

    with transaction.atomic():
        job = (
            ImportJob.objects.select_for_update()
            .filter(workspace_id=workspace_id, pk=job_id)
            .first()
        )
        if job is None:
            raise NotFoundError("import not found")
        if job.status != JobStatus.UPLOADED:
            raise ConflictError("import already started", errors={"import": "already_started"})
        _check_mapping(workspace_id, job.headers, mapping)
        job.mapping, job.options, job.status = mapping, _check_options(options), JobStatus.QUEUED
        job.save()
        record_event(
            action="crm.import.started",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_import",
            target_id=job.pk,
        )
        transaction.on_commit(
            lambda: run_import_task.delay(workspace_id=str(workspace_id), job_id=str(job_id))
        )
    return job


def run(*, workspace_id: uuid.UUID, job_id: uuid.UUID) -> ImportJob:
    """Process the file from where it stopped. Safe to call again after a crash."""
    job = get_job(workspace_id, job_id)
    if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
        return job
    job.status = JobStatus.RUNNING
    job.save(update_fields=["status", "updated_at"])
    actor = Actor.human(job.created_by_id) if job.created_by_id else Actor.system("import")
    rows = csvio.read_rows(job.content)
    next(rows, None)
    index = 0
    for number, row in enumerate(rows, start=2):
        if not any(c.strip() for c in row):
            continue
        index += 1
        if index <= job.processed_rows:
            continue
        _process_row(job, actor, number, row)
        job.processed_rows = index
        if index % settings.CRM_BULK_CHUNK_SIZE == 0:
            job.save()
    job.status, job.finished_at, job.content = JobStatus.DONE, timezone.now(), ""
    job.save()
    return job


def _process_row(job: ImportJob, actor: Actor, number: int, row: list[str]) -> None:
    workspace_id = job.workspace_id
    data = _row_data(job.headers, job.mapping, row)
    tags = [*job.options.get("tags", []), *data.get("tags", [])]
    if tags:
        data["tags"] = tags
    try:
        with transaction.atomic():
            if job.options.get("update_existing", True):
                contact, created = contacts.upsert_contact(
                    workspace_id=workspace_id, actor=actor, data=data
                )
            else:
                contact, created = (
                    contacts.create_contact(workspace_id=workspace_id, actor=actor, data=data),
                    True,
                )
            wanted = job.options.get("consent")
            if wanted:
                consent.record(
                    workspace_id=workspace_id,
                    actor=actor,
                    contact_id=contact.pk,
                    channel=wanted["channel"],
                    purpose=wanted["purpose"],
                    status="opted_in",
                    source="import",
                    evidence={"import_id": str(job.pk), "file": job.file_name},
                )
        job.created_count += int(created)
        job.updated_count += int(not created)
    except (ValidationFailedError, ConflictError) as exc:
        job.failed_count += 1
        if len(job.errors) < settings.CRM_IMPORT_ERROR_LIMIT:
            job.errors.append(
                {"row": number, "errors": exc.params.get("errors", {}), "values": row}
            )


def error_rows(job: ImportJob) -> list[list[str]]:
    """Rows for the downloadable error file: original columns plus the reasons."""
    out = [[*job.headers, "row", "errors"]]
    for item in job.errors:
        reasons = "; ".join(f"{k}:{v}" for k, v in item["errors"].items())
        out.append([*item["values"], str(item["row"]), reasons])
    return out


def expire_old(*, older_than: timedelta) -> int:
    """Housekeeping: drop the content of abandoned uploads."""
    cutoff = timezone.now() - older_than
    return ImportJob.objects.filter(status=JobStatus.UPLOADED, created_at__lt=cutoff).update(
        content="", status=JobStatus.CANCELED
    )
