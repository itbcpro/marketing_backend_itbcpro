"""Social domain: the local copy of an Instagram account and everything the AI works on.

Sync keeps accounts, media, comments and conversations current; screens and agents read the local
copy. Anything that changes the outside world goes through the action layer, never through these
models directly.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class MediaKind(models.TextChoices):
    IMAGE = "image"
    CAROUSEL = "carousel"
    VIDEO = "video"
    REEL = "reel"
    STORY = "story"


class SocialAccount(TenantModel):
    """One Instagram professional account behind a connection."""

    connection = models.OneToOneField(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    network = models.CharField(max_length=20, default="instagram")
    external_id = models.CharField(max_length=100)
    username = models.CharField(max_length=100, blank=True, default="")
    name = models.CharField(max_length=200, blank=True, default="")
    account_type = models.CharField(max_length=30, blank=True, default="")
    profile_picture_url = models.CharField(max_length=2048, blank=True, default="")
    biography = models.TextField(blank=True, default="")
    website = models.CharField(max_length=500, blank=True, default="")
    followers_count = models.BigIntegerField(default=0)
    follows_count = models.BigIntegerField(default=0)
    media_count = models.BigIntegerField(default=0)
    audience = models.JSONField(default=dict)
    audience_synced_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    counts = models.JSONField(default=dict)
    webhooks_subscribed = models.BooleanField(default=False)

    objects = TenantManager["SocialAccount"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "external_id"], name="uniq_social_account")
        ]

    def __str__(self) -> str:
        return f"@{self.username}" if self.username else self.external_id


class SocialMedia(TenantModel):
    """A published post, reel, video, carousel or live story of the account."""

    account = models.ForeignKey(SocialAccount, on_delete=models.CASCADE, related_name="media")
    external_id = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=MediaKind.choices)
    caption = models.TextField(blank=True, default="")
    permalink = models.CharField(max_length=500, blank=True, default="")
    media_url = models.CharField(max_length=2048, blank=True, default="")
    thumbnail_url = models.CharField(max_length=2048, blank=True, default="")
    posted_at = models.DateTimeField()
    like_count = models.IntegerField(default=0)
    comments_count = models.IntegerField(default=0)
    comments_enabled = models.BooleanField(default=True)
    children = models.JSONField(default=list)
    metrics = models.JSONField(default=dict)
    metrics_synced_at = models.DateTimeField(null=True, blank=True)
    analysis = models.JSONField(null=True, blank=True)
    analysis_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["SocialMedia"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "external_id"], name="uniq_social_media"),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in MediaKind]), name="social_media_kind_valid"
            ),
        ]
        indexes = [models.Index(fields=["account", "-posted_at"], name="social_media_recent_idx")]
        ordering = ["-posted_at"]


class MediaMetricSnapshot(TenantModel):
    """Metrics of one post on one day, kept to read 24 hour, 7 day and 28 day outcomes."""

    media = models.ForeignKey(SocialMedia, on_delete=models.CASCADE, related_name="snapshots")
    day = models.DateField()
    age_hours = models.IntegerField(default=0)
    metrics = models.JSONField(default=dict)

    objects = TenantManager["MediaMetricSnapshot"]()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["media", "day"], name="uniq_media_snapshot")]
        indexes = [models.Index(fields=["media", "-day"], name="media_snapshot_idx")]


class AccountInsight(TenantModel):
    """Account totals over a trailing window plus daily series, one row per sync day."""

    account = models.ForeignKey(SocialAccount, on_delete=models.CASCADE, related_name="insights")
    day = models.DateField()
    window_days = models.PositiveSmallIntegerField(default=28)
    followers_count = models.BigIntegerField(default=0)
    totals = models.JSONField(default=dict)
    series = models.JSONField(default=dict)

    objects = TenantManager["AccountInsight"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "day"], name="uniq_account_insight")
        ]
        indexes = [models.Index(fields=["account", "-day"], name="account_insight_idx")]
        ordering = ["-day"]


class CommentStatus(models.TextChoices):
    NEW = "new"
    DRAFT_READY = "draft_ready"
    REPLIED = "replied"
    HIDDEN = "hidden"
    ESCALATED = "escalated"
    IGNORED = "ignored"


class SocialComment(TenantModel):
    account = models.ForeignKey(SocialAccount, on_delete=models.CASCADE, related_name="comments")
    media = models.ForeignKey(
        SocialMedia, null=True, blank=True, on_delete=models.CASCADE, related_name="comments"
    )
    external_id = models.CharField(max_length=100)
    parent_external_id = models.CharField(max_length=100, blank=True, default="")
    username = models.CharField(max_length=100, blank=True, default="")
    author_id = models.CharField(max_length=100, blank=True, default="")
    text = models.TextField(blank=True, default="")
    like_count = models.IntegerField(default=0)
    hidden = models.BooleanField(default=False)
    from_owner = models.BooleanField(default=False)
    posted_at = models.DateTimeField()
    status = models.CharField(max_length=12, choices=CommentStatus.choices, default="new")
    triaged_at = models.DateTimeField(null=True, blank=True)
    category = models.CharField(max_length=20, blank=True, default="")
    sentiment = models.CharField(max_length=10, blank=True, default="")
    language = models.CharField(max_length=20, blank=True, default="")
    priority = models.PositiveSmallIntegerField(default=0)
    confidence = models.FloatField(default=0.0)
    reason = models.CharField(max_length=300, blank=True, default="")
    suggested_action = models.CharField(max_length=10, blank=True, default="")
    auto_eligible = models.BooleanField(default=False)
    draft = models.TextField(blank=True, default="")
    reply_text = models.TextField(blank=True, default="")
    reply_external_id = models.CharField(max_length=100, blank=True, default="")
    handled_by = models.CharField(max_length=10, blank=True, default="")
    handled_at = models.DateTimeField(null=True, blank=True)
    action_id = models.UUIDField(null=True, blank=True)

    objects = TenantManager["SocialComment"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "external_id"], name="uniq_social_comment"),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in CommentStatus]),
                name="social_comment_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["account", "status", "-posted_at"], name="social_comment_inbox_idx"
            ),
            models.Index(fields=["media", "-posted_at"], name="social_comment_media_idx"),
        ]
        ordering = ["-posted_at"]


class ConversationStatus(models.TextChoices):
    OPEN = "open"
    HANDOFF = "handoff"
    CLOSED = "closed"
    OPTED_OUT = "opted_out"


class SocialConversation(TenantModel):
    """A direct-message thread with one person."""

    account = models.ForeignKey(
        SocialAccount, on_delete=models.CASCADE, related_name="conversations"
    )
    external_id = models.CharField(max_length=100)
    participant_id = models.CharField(max_length=100, blank=True, default="")
    participant_username = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=10, choices=ConversationStatus.choices, default="open")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    bot_paused = models.BooleanField(default=False)
    unread = models.BooleanField(default=False)
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_outbound_at = models.DateTimeField(null=True, blank=True)
    needs_reply = models.BooleanField(default=False)
    analysed_through = models.DateTimeField(null=True, blank=True)
    intent = models.CharField(max_length=20, blank=True, default="")
    urgency = models.CharField(max_length=10, blank=True, default="")
    sentiment = models.CharField(max_length=10, blank=True, default="")
    language = models.CharField(max_length=20, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    draft_reply = models.TextField(blank=True, default="")
    handoff_reason = models.CharField(max_length=300, blank=True, default="")
    lead = models.JSONField(default=dict)

    objects = TenantManager["SocialConversation"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "external_id"], name="uniq_social_conversation"
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in ConversationStatus]),
                name="social_conversation_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["account", "status", "-last_message_at"], name="social_dm_inbox_idx"
            )
        ]
        ordering = ["-last_message_at"]


class SocialMessage(TenantModel):
    conversation = models.ForeignKey(
        SocialConversation, on_delete=models.CASCADE, related_name="messages"
    )
    external_id = models.CharField(max_length=200)
    inbound = models.BooleanField(default=True)
    author = models.CharField(max_length=10, default="user")
    text = models.TextField(blank=True, default="")
    attachments = models.JSONField(default=list)
    sent_at = models.DateTimeField()

    objects = TenantManager["SocialMessage"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "external_id"], name="uniq_social_message"
            )
        ]
        indexes = [models.Index(fields=["conversation", "sent_at"], name="social_message_idx")]
        ordering = ["sent_at"]


class AccountSettings(TenantModel):
    """Response settings of one account (comments and messages), validated by ``ReplySettings``."""

    account = models.OneToOneField(SocialAccount, on_delete=models.CASCADE, related_name="settings")
    config = models.JSONField(default=dict)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["AccountSettings"]()


class DraftStatus(models.TextChoices):
    IDEA = "idea"
    DRAFTED = "drafted"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PostDraft(TenantModel):
    """A post being planned, written, approved and published."""

    account = models.ForeignKey(SocialAccount, on_delete=models.CASCADE, related_name="drafts")
    kind = models.CharField(max_length=10, choices=MediaKind.choices, default=MediaKind.IMAGE)
    status = models.CharField(max_length=12, choices=DraftStatus.choices, default="drafted")
    title = models.CharField(max_length=200, blank=True, default="")
    brief = models.TextField(blank=True, default="")
    caption = models.TextField(blank=True, default="")
    hashtags = models.JSONField(default=list)
    first_comment = models.TextField(blank=True, default="")
    media = models.JSONField(default=list)
    cover_asset_id = models.UUIDField(null=True, blank=True)
    share_to_feed = models.BooleanField(default=True)
    location_id = models.CharField(max_length=50, blank=True, default="")
    collaborators = models.JSONField(default=list)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    pillar = models.CharField(max_length=30, blank=True, default="")
    origin = models.CharField(max_length=10, default="user")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    ai = models.JSONField(default=dict)
    ai_status = models.CharField(max_length=10, blank=True, default="")
    ai_error_code = models.CharField(max_length=60, blank=True, default="")
    ai_since = models.DateTimeField(null=True, blank=True)
    checks = models.JSONField(default=list)
    action_id = models.UUIDField(null=True, blank=True)
    publish_state = models.JSONField(default=dict)
    external_media_id = models.CharField(max_length=100, blank=True, default="")
    permalink = models.CharField(max_length=500, blank=True, default="")
    published_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    error_detail = models.CharField(max_length=500, blank=True, default="")

    objects = TenantManager["PostDraft"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in DraftStatus]),
                name="post_draft_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in MediaKind]), name="post_draft_kind_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["account", "status", "scheduled_at"], name="post_draft_board_idx")
        ]
        ordering = ["-created_at"]


class ReportScope(models.TextChoices):
    ACCOUNT = "account"
    MEDIA = "media"
    PLAN = "plan"


class SocialReport(TenantModel):
    """One AI analysis: of the whole account, of a single post, or a content plan."""

    account = models.ForeignKey(SocialAccount, on_delete=models.CASCADE, related_name="reports")
    scope = models.CharField(max_length=10, choices=ReportScope.choices)
    media = models.ForeignKey(
        SocialMedia, null=True, blank=True, on_delete=models.CASCADE, related_name="reports"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=10, default="thinking")
    thinking_since = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    period_days = models.PositiveSmallIntegerField(default=90)
    instruction = models.TextField(blank=True, default="")
    stats = models.JSONField(default=dict)
    result = models.JSONField(null=True, blank=True)
    previous = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = TenantManager["SocialReport"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["thinking", "done", "failed"]),
                name="social_report_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(scope__in=[s.value for s in ReportScope]),
                name="social_report_scope_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "scope", "-created_at"], name="social_report_idx")
        ]
        ordering = ["-created_at"]


class WebhookEvent(TenantModel):
    """A delivery from Instagram, stored first and processed afterwards. Duplicates are dropped."""

    account = models.ForeignKey(
        SocialAccount, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    dedupe_key = models.CharField(max_length=64)
    field = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True, default="")

    objects = TenantManager["WebhookEvent"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dedupe_key"], name="uniq_social_webhook_event")
        ]
        indexes = [models.Index(fields=["processed_at", "created_at"], name="social_webhook_idx")]
