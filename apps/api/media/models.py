from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class MediaKind(models.TextChoices):
    IMAGE = "image"
    VIDEO = "video"


class MediaSource(models.TextChoices):
    UPLOAD = "upload"
    URL = "url"
    YOUTUBE = "youtube"
    DERIVED = "derived"
    GENERATED = "generated"


class Rights(models.TextChoices):
    OWNED = "owned"
    LICENSED = "licensed"
    STOCK = "stock"
    UNKNOWN = "unknown"


class MediaAsset(TenantModel):
    """An image or video the company can use in any channel.

    Files live in private storage and are only served through authenticated endpoints. A YouTube
    video is a link (Google Ads video assets must be YouTube videos), so it has no file.
    """

    kind = models.CharField(max_length=10, choices=MediaKind.choices)
    source = models.CharField(max_length=10, choices=MediaSource.choices)
    name = models.CharField(max_length=300)
    description = models.TextField(blank=True, default="")
    alt_text = models.CharField(max_length=500, blank=True, default="")
    tags = models.JSONField(default=list)
    storage_key = models.CharField(max_length=300, blank=True, default="")
    original_filename = models.CharField(max_length=300, blank=True, default="")
    mime_type = models.CharField(max_length=100, blank=True, default="")
    file_format = models.CharField(max_length=20, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    animated = models.BooleanField(default=False)
    has_alpha = models.BooleanField(default=False)
    youtube_video_id = models.CharField(max_length=20, blank=True, default="")
    external = models.JSONField(default=dict)
    rights = models.CharField(max_length=10, choices=Rights.choices, default=Rights.UNKNOWN)
    license_note = models.CharField(max_length=500, blank=True, default="")
    license_expires_on = models.DateField(null=True, blank=True)
    ai_generated = models.BooleanField(default=False)
    derived_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="derivatives"
    )
    variant = models.CharField(max_length=40, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["MediaAsset"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "sha256"],
                condition=~Q(sha256=""),
                name="uniq_media_hash_per_workspace",
            ),
            models.UniqueConstraint(
                fields=["workspace", "youtube_video_id"],
                condition=~Q(youtube_video_id=""),
                name="uniq_media_youtube_per_workspace",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in MediaKind]), name="media_kind_valid"
            ),
        ]
        indexes = [models.Index(fields=["workspace", "kind", "-created_at"], name="media_list_idx")]
        ordering = ["-created_at"]
