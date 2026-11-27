"""Smart lists, CSV imports and bulk jobs."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class Visibility(models.TextChoices):
    PRIVATE = "private"
    SHARED = "shared"


class SmartList(TenantModel):
    """A saved dynamic filter with its columns. Membership is recomputed on every read."""

    name = models.CharField(max_length=100)
    filter = models.JSONField(default=dict)
    columns = models.JSONField(default=list)
    visibility = models.CharField(
        max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["SmartList"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(visibility__in=[v.value for v in Visibility]),
                name="smart_list_visibility_valid",
            )
        ]
        ordering = ["name"]


class JobStatus(models.TextChoices):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class ImportKind(models.TextChoices):
    CONTACTS = "contacts"
    DEALS = "deals"


class ImportJob(TenantModel):
    """One CSV import. The file is kept until the job ends so a crashed run can resume."""

    kind = models.CharField(max_length=10, choices=ImportKind.choices, default=ImportKind.CONTACTS)
    file_name = models.CharField(max_length=200)
    content = models.TextField()
    headers = models.JSONField(default=list)
    mapping = models.JSONField(default=dict)
    options = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=JobStatus.choices, default=JobStatus.UPLOADED)
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["ImportJob"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in JobStatus]), name="import_status_valid"
            )
        ]
        ordering = ["-created_at"]


class BulkAction(models.TextChoices):
    ADD_TAGS = "add_tags"
    REMOVE_TAGS = "remove_tags"
    ASSIGN = "assign"
    DELETE = "delete"


class BulkJob(TenantModel):
    """A bulk action over a snapshot of contact ids, processed in chunks with per-record results."""

    action = models.CharField(max_length=20, choices=BulkAction.choices)
    params = models.JSONField(default=dict)
    contact_ids = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=JobStatus.choices, default=JobStatus.QUEUED)
    total = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    succeeded = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    failures = models.JSONField(default=list)
    cancel_requested = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["BulkJob"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(action__in=[a.value for a in BulkAction]), name="bulk_action_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in JobStatus]), name="bulk_status_valid"
            ),
        ]
        ordering = ["-created_at"]
