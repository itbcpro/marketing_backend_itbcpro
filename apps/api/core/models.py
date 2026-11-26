"""Abstract base models and cross-cutting core tables."""

from __future__ import annotations

import uuid
from typing import Any, Protocol, Self

from django.db import models
from django.db.models import Q

from core.ids import new_id


class HasPk(Protocol):
    pk: Any


class BaseModel(models.Model):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantQuerySet[T: models.Model](models.QuerySet[T]):
    def for_workspace(self, workspace: HasPk | uuid.UUID) -> Self:
        workspace_id = workspace if isinstance(workspace, uuid.UUID) else workspace.pk
        return self.filter(workspace_id=workspace_id)


class TenantManager[T: models.Model](models.Manager[T]):
    def get_queryset(self) -> TenantQuerySet[T]:
        return TenantQuerySet(self.model, using=self._db)

    def for_workspace(self, workspace: HasPk | uuid.UUID) -> TenantQuerySet[T]:
        return self.get_queryset().for_workspace(workspace)


class TenantModel(BaseModel):
    """Base for every table owned by a workspace. Query through ``for_workspace``."""

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.PROTECT, related_name="+", db_index=True
    )

    class Meta:
        abstract = True


class FeatureFlag(BaseModel):
    """Feature flag with an optional per-workspace override (workspace null = global default)."""

    name = models.CharField(max_length=100)
    workspace = models.ForeignKey(
        "workspaces.Workspace", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    enabled = models.BooleanField(default=False)
    description = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["name"], condition=Q(workspace__isnull=True), name="uniq_flag_global"
            ),
            models.UniqueConstraint(
                fields=["name", "workspace"],
                condition=Q(workspace__isnull=False),
                name="uniq_flag_workspace",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name}={self.enabled}"


class DeadLetter(BaseModel):
    """A task that exhausted its retries or failed permanently; kept for inspection and replay."""

    task_name = models.CharField(max_length=200, db_index=True)
    task_id = models.CharField(max_length=100, blank=True, default="")
    args = models.JSONField(default=list)
    kwargs = models.JSONField(default=dict)
    error = models.TextField()
    traceback = models.TextField(blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["created_at"],
                condition=Q(resolved_at__isnull=True),
                name="deadletter_open_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.task_name} ({self.task_id})"


class IdempotencyRecord(BaseModel):
    """Stored outcome of an unsafe request so retries with the same key are replayed."""

    class State(models.TextChoices):
        IN_PROGRESS = "in_progress"
        COMPLETED = "completed"

    user_id = models.UUIDField(db_index=True)
    key = models.CharField(max_length=200)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=500)
    request_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=20, choices=State.choices, default=State.IN_PROGRESS)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.BinaryField(null=True, blank=True)
    response_content_type = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user_id", "key"], name="uniq_idempotency_user_key"),
            models.CheckConstraint(
                condition=Q(state__in=["in_progress", "completed"]), name="idempotency_state_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key} ({self.state})"
