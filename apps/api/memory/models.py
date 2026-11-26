"""Company memory: brand profile, learned rules and preferences, feedback and their evidence."""

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models
from django.db.models import Q
from pgvector.django import HnswIndex, VectorField

from core.models import TenantManager, TenantModel

EMBEDDING_DIMENSIONS = 1024


class BrandProfile(TenantModel):
    """Owner-defined identity. Every save creates a new version; the newest is current."""

    version = models.PositiveIntegerField()
    content = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["BrandProfile"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "version"], name="uniq_brand_version"),
        ]
        ordering = ["-version"]


class MemoryKind(models.TextChoices):
    RULE = "rule"
    PREFERENCE = "preference"
    FACT = "fact"
    OUTCOME = "outcome"
    EXPERIMENT_RESULT = "experiment_result"


class MemoryScope(models.TextChoices):
    WORKSPACE = "workspace"
    CHANNEL = "channel"
    AGENT = "agent"
    CAMPAIGN = "campaign"


class MemoryStatus(models.TextChoices):
    PROPOSED = "proposed"
    ACTIVE = "active"
    RETIRED = "retired"
    REJECTED = "rejected"


class MemoryItem(TenantModel):
    kind = models.CharField(max_length=20, choices=MemoryKind.choices)
    scope = models.CharField(
        max_length=20, choices=MemoryScope.choices, default=MemoryScope.WORKSPACE
    )
    scope_ref = models.CharField(max_length=100, blank=True, default="")
    text = models.TextField()
    structured = models.JSONField(default=dict)
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    confidence = models.FloatField(default=0.5)
    status = models.CharField(
        max_length=10, choices=MemoryStatus.choices, default=MemoryStatus.PROPOSED
    )
    is_hard_rule = models.BooleanField(
        default=False, help_text="Always included in agent context, regardless of relevance."
    )
    source = models.CharField(max_length=20, default="user")
    evidence = models.JSONField(default=list)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_to = models.DateTimeField(null=True, blank=True)
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="superseded_by"
    )
    usage_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["MemoryItem"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(confidence__gte=0.0, confidence__lte=1.0),
                name="memory_confidence_range",
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in MemoryStatus]), name="memory_status_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "status", "scope"], name="memory_lookup_idx"),
            GinIndex(SearchVector("text", config="english"), name="memory_fts_idx"),
            HnswIndex(
                name="memory_embedding_idx",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.text[:60]}"


class FeedbackEvent(TenantModel):
    """One human reaction to an agent proposal. Raw material for learning."""

    action_id = models.UUIDField(null=True, blank=True, db_index=True)
    agent = models.CharField(max_length=100)
    action_kind = models.CharField(max_length=100, blank=True, default="")
    verdict = models.CharField(max_length=20)
    reason = models.CharField(max_length=40, blank=True, default="")
    note = models.TextField(blank=True, default="")
    proposed = models.JSONField(default=dict)
    final = models.JSONField(default=dict)
    changed_fields = models.JSONField(default=list)
    processed = models.BooleanField(default=False)

    objects = TenantManager["FeedbackEvent"]()

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "processed", "created_at"], name="feedback_todo_idx")
        ]

    def __str__(self) -> str:
        return f"{self.agent} {self.verdict}"
