"""Content studio tables: strategy, trend signals, ideas, pieces of content and daily runs.

A *piece* is one post for one channel. Ideas are channel-neutral angles; one idea can become a
piece on several channels. Publishing never happens here: pieces are handed to a publisher (for
Instagram, the existing post pipeline with its approval, audit and kill switch) or to a person.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class PieceKind(models.TextChoices):
    TEXT = "text"
    IMAGE = "image"
    CAROUSEL = "carousel"
    VIDEO = "video"
    STORY = "story"


class PieceStatus(models.TextChoices):
    DRAFTING = "drafting"
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    MANUAL_DUE = "manual_due"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IdeaStatus(models.TextChoices):
    NEW = "new"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    USED = "used"


class RunKind(models.TextChoices):
    DAILY = "daily"
    MANUAL = "manual"
    IDEAS = "ideas"


class RunStatus(models.TextChoices):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Brand(TenantModel):
    """One company or page the studio works for. Everything the studio keeps belongs to a brand."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Brand"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "slug"], name="uniq_studio_brand_slug")
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class StudioProfile(TenantModel):
    """The studio settings of one brand (validated by ``config.StudioConfig``)."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="+")
    config = models.JSONField(default=dict)
    automation_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["StudioProfile"]()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["brand"], name="uniq_studio_profile_brand")]


class TrendSource(TenantModel):
    """Where trends come from: a feed, a trending list, the site's own search queries."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=30)
    name = models.CharField(max_length=120)
    params = models.JSONField(default=dict)
    enabled = models.BooleanField(default=True)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    last_count = models.IntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True, default="")

    objects = TenantManager["TrendSource"]()

    class Meta:
        indexes = [models.Index(fields=["workspace", "enabled"], name="studio_source_idx")]
        ordering = ["name"]


class TrendItem(TenantModel):
    """One thing that is happening: a headline, a rising search, a discussion."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="+")
    source = models.ForeignKey(
        TrendSource, null=True, blank=True, on_delete=models.SET_NULL, related_name="items"
    )
    kind = models.CharField(max_length=30)
    title = models.CharField(max_length=300)
    summary = models.TextField(blank=True, default="")
    url = models.CharField(max_length=1000, blank=True, default="")
    fingerprint = models.CharField(max_length=40)
    score = models.FloatField(default=0.0)
    published_at = models.DateTimeField(null=True, blank=True)
    ignored = models.BooleanField(default=False)

    objects = TenantManager["TrendItem"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["brand", "fingerprint"], name="uniq_studio_trend_brand")
        ]
        indexes = [models.Index(fields=["brand", "-created_at"], name="studio_trend_brand_idx")]
        ordering = ["-created_at"]


class ContentIdea(TenantModel):
    """A channel-neutral angle: what to say and why now."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="+")
    title = models.CharField(max_length=200)
    angle = models.TextField(blank=True, default="")
    why_now = models.TextField(blank=True, default="")
    pillar = models.CharField(max_length=40, blank=True, default="")
    urgency = models.CharField(max_length=10, default="evergreen")
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=IdeaStatus.choices, default=IdeaStatus.NEW)
    score = models.FloatField(default=0.0)
    format_hints = models.JSONField(default=list)
    trend_item_ids = models.JSONField(default=list)
    needs_from_owner = models.CharField(max_length=500, blank=True, default="")
    origin = models.CharField(max_length=10, default="ai")

    objects = TenantManager["ContentIdea"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in IdeaStatus]),
                name="studio_idea_status_valid",
            )
        ]
        indexes = [models.Index(fields=["brand", "status", "-score"], name="studio_idea_brand_idx")]
        ordering = ["-created_at"]


class ContentPiece(TenantModel):
    """One post for one channel, from first draft to published (or handed to a person)."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="+")
    idea = models.ForeignKey(
        ContentIdea, null=True, blank=True, on_delete=models.SET_NULL, related_name="pieces"
    )
    channel = models.CharField(max_length=20)
    kind = models.CharField(max_length=10, choices=PieceKind.choices)
    status = models.CharField(max_length=12, choices=PieceStatus.choices, default="drafting")
    title = models.CharField(max_length=200, blank=True, default="")
    brief = models.TextField(blank=True, default="")
    pillar = models.CharField(max_length=40, blank=True, default="")
    copy = models.JSONField(default=dict)
    media = models.JSONField(default=list)
    cover_asset_id = models.UUIDField(null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    origin = models.CharField(max_length=10, default="ai")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    ai_status = models.CharField(max_length=10, blank=True, default="")
    ai_error_code = models.CharField(max_length=60, blank=True, default="")
    ai_since = models.DateTimeField(null=True, blank=True)
    ai_state = models.JSONField(default=dict)
    checks = models.JSONField(default=list)
    external = models.JSONField(default=dict)
    permalink = models.CharField(max_length=500, blank=True, default="")
    published_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    error_detail = models.CharField(max_length=500, blank=True, default="")

    objects = TenantManager["ContentPiece"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in PieceStatus]),
                name="studio_piece_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in PieceKind]), name="studio_piece_kind_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["brand", "status", "scheduled_at"], name="studio_piece_brand_idx"),
            models.Index(fields=["brand", "channel", "scheduled_at"], name="studio_slot_brand_idx"),
        ]
        ordering = ["scheduled_at", "-created_at"]


class StudioRun(TenantModel):
    """One research-and-planning pass, kept so the owner can see what the routine did."""

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=RunKind.choices)
    status = models.CharField(max_length=10, choices=RunStatus.choices, default="running")
    local_date = models.DateField()
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    stats = models.JSONField(default=dict)
    error_code = models.CharField(max_length=60, blank=True, default="")

    objects = TenantManager["StudioRun"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in RunStatus]),
                name="studio_run_status_valid",
            )
        ]
        indexes = [models.Index(fields=["brand", "-started_at"], name="studio_run_brand_idx")]
        ordering = ["-started_at"]
