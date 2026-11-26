"""Notes, tasks and the append-only activity timeline of a contact."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class Note(TenantModel):
    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="notes"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    opportunity = models.ForeignKey(
        "crm_pipelines.Opportunity",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The deal this note is about, when it is about one.",
    )
    body = models.TextField()
    pinned = models.BooleanField(default=False)
    mentions = models.JSONField(default=list)
    edited_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Note"]()

    class Meta:
        indexes = [
            models.Index(fields=["contact", "-created_at"], name="note_contact_idx"),
            models.Index(fields=["opportunity", "-created_at"], name="note_deal_idx"),
        ]
        ordering = ["-pinned", "-created_at"]


class TaskStatus(models.TextChoices):
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


class Task(TenantModel):
    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="tasks"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    opportunity = models.ForeignKey(
        "crm_pipelines.Opportunity",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The deal this task is about, when it is about one.",
    )
    due_at = models.DateTimeField(null=True, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(
        max_length=12, choices=TaskStatus.choices, default=TaskStatus.INCOMPLETE
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["Task"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in TaskStatus]), name="task_status_valid"
            )
        ]
        indexes = [
            models.Index(
                fields=["workspace", "assigned_to", "status", "due_at"], name="task_inbox_idx"
            ),
            models.Index(fields=["contact", "status"], name="task_contact_idx"),
            models.Index(fields=["opportunity", "status"], name="task_deal_idx"),
        ]


class AppointmentStatus(models.TextChoices):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class Appointment(TenantModel):
    """A meeting with a contact, optionally about one of their deals."""

    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="appointments"
    )
    opportunity = models.ForeignKey(
        "crm_pipelines.Opportunity",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The deal this appointment is about, when it is about one.",
    )
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    location = models.CharField(max_length=300, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(
        max_length=12, choices=AppointmentStatus.choices, default=AppointmentStatus.SCHEDULED
    )

    objects = TenantManager["Appointment"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in AppointmentStatus]),
                name="appointment_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")), name="appointment_ends_after_start"
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "starts_at"], name="appt_workspace_start_idx"),
            models.Index(fields=["contact", "starts_at"], name="appt_contact_idx"),
            models.Index(fields=["opportunity", "starts_at"], name="appt_deal_idx"),
        ]
        ordering = ["starts_at"]


class Activity(TenantModel):
    """One thing that happened to a contact. Written by event consumers; never edited."""

    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="activities"
    )
    kind = models.CharField(max_length=60)
    data = models.JSONField(default=dict)
    actor_type = models.CharField(max_length=20)
    actor_id = models.CharField(max_length=100)
    occurred_at = models.DateTimeField()
    event_id = models.UUIDField(null=True, blank=True)

    objects = TenantManager["Activity"]()

    class Meta:
        indexes = [models.Index(fields=["contact", "-id"], name="activity_contact_idx")]

    def save(self, *args: object, **kwargs: object) -> None:
        if not self._state.adding:
            raise RuntimeError("activities are append-only")
        super().save(*args, **kwargs)  # type: ignore[arg-type]
