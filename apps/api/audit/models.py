"""Append-only, hash-chained audit log and the transactional outbox."""

import uuid

from django.db import models
from django.db.models import Q

from core.ids import new_id

SYSTEM_CHAIN = uuid.UUID(int=0)


class ActorType(models.TextChoices):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class AuditEvent(models.Model):
    """Immutable record. Updates and deletes are rejected by database triggers."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    chain = models.UUIDField(help_text="Workspace id, or the nil UUID for system-wide events.")
    seq = models.PositiveBigIntegerField()
    occurred_at = models.DateTimeField()
    workspace_id = models.UUIDField(null=True, blank=True, db_index=True)
    actor_type = models.CharField(max_length=10, choices=ActorType.choices)
    actor_id = models.CharField(max_length=100, blank=True, default="")
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=100, blank=True, default="")
    target_id = models.CharField(max_length=100, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    prev_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["chain", "seq"], name="uniq_audit_chain_seq"),
            models.CheckConstraint(
                condition=Q(actor_type__in=["human", "agent", "system"]),
                name="audit_actor_type_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
            models.Index(fields=["occurred_at"], name="audit_occurred_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.chain}#{self.seq} {self.action}"


class AuditCheckpoint(models.Model):
    """Anchor left behind when old events are pruned so the remaining chain still verifies."""

    chain = models.UUIDField(primary_key=True)
    last_seq = models.PositiveBigIntegerField()
    last_hash = models.CharField(max_length=64)
    events_pruned = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.chain} up to #{self.last_seq}"


class OutboxMessage(models.Model):
    """Message written in the same transaction as the state change it announces."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    topic = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    available_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(
                fields=["available_at"],
                condition=Q(processed_at__isnull=True),
                name="outbox_pending_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.topic} ({self.pk})"
