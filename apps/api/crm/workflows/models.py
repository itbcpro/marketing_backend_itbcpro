"""Workflows, their immutable versions, who is going through them, and what happened."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class WorkflowStatus(models.TextChoices):
    DRAFT = "draft"  # no version published yet
    ACTIVE = "active"
    PAUSED = "paused"  # nobody new enters; people already inside carry on


class Workflow(TenantModel):
    name = models.CharField(max_length=100)
    status = models.CharField(
        max_length=10, choices=WorkflowStatus.choices, default=WorkflowStatus.DRAFT
    )
    reenter = models.BooleanField(default=False)
    stop_on_reply = models.BooleanField(default=True)
    published_version = models.ForeignKey(
        "WorkflowVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Workflow"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                condition=Q(archived_at__isnull=True),
                name="uniq_workflow_name",
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in WorkflowStatus]),
                name="workflow_status_valid",
            ),
        ]
        ordering = ["created_at"]


class WorkflowVersion(TenantModel):
    """One definition. A published one never changes: people inside keep the version they joined."""

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    definition = models.JSONField(default=dict)
    published_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["WorkflowVersion"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workflow", "number"], name="uniq_workflow_version")
        ]
        ordering = ["-number"]


class EnrollmentStatus(models.TextChoices):
    ACTIVE = "active"  # ready to run its next step
    WAITING = "waiting"  # until a time, or until something happens
    COMPLETED = "completed"
    EXITED = "exited"  # stopped on purpose: replied, paused by a person, deleted
    FAILED = "failed"


OPEN_STATUSES = (EnrollmentStatus.ACTIVE, EnrollmentStatus.WAITING)


class Enrollment(TenantModel):
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="enrollments")
    version = models.ForeignKey(WorkflowVersion, on_delete=models.PROTECT, related_name="+")
    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="enrollments"
    )
    status = models.CharField(
        max_length=10, choices=EnrollmentStatus.choices, default=EnrollmentStatus.ACTIVE
    )
    current_step = models.CharField(max_length=60)
    wake_at = models.DateTimeField(null=True, blank=True)
    waiting_for = models.CharField(max_length=60, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    context = models.JSONField(default=dict)
    exit_reason = models.CharField(max_length=60, blank=True, default="")
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Enrollment"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "contact"],
                condition=Q(status__in=["active", "waiting"]),
                name="uniq_open_enrollment",
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in EnrollmentStatus]),
                name="enrollment_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["wake_at"],
                condition=Q(status__in=["active", "waiting"]),
                name="enrollment_due_idx",
            ),
            models.Index(
                fields=["workspace", "contact", "-created_at"], name="enrollment_contact_idx"
            ),
        ]
        ordering = ["-created_at", "-id"]


class StepOutcome(models.TextChoices):
    DONE = "done"
    WAITING = "waiting"
    SKIPPED = "skipped"
    RETRY = "retry"
    FAILED = "failed"


class StepLog(TenantModel):
    """The execution log of one contact in one workflow, step by step."""

    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="log")
    step_id = models.CharField(max_length=60)
    step_type = models.CharField(max_length=40)
    outcome = models.CharField(max_length=10, choices=StepOutcome.choices)
    detail = models.CharField(max_length=300, blank=True, default="")
    attempt = models.PositiveSmallIntegerField(default=1)

    objects = TenantManager["StepLog"]()

    class Meta:
        indexes = [models.Index(fields=["enrollment", "created_at"], name="steplog_enrollment_idx")]
        ordering = ["created_at", "id"]
