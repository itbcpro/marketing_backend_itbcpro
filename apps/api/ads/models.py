from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel


class AssistStatus(models.TextChoices):
    THINKING = "thinking"
    NEEDS_INPUT = "needs_input"
    DRAFT = "draft"
    FAILED = "failed"
    LAUNCHED = "launched"
    ARCHIVED = "archived"


class AssistSession(TenantModel):
    """One saved conversation with the ad assistant.

    ``history`` is the ordered event log shown as the thread (goal, questions, answers, drafts,
    feedback, errors, launch). ``questions`` and ``draft`` hold the latest state so the screen can
    resume exactly where the owner left it, even after a refresh or on another device.
    """

    connection = models.ForeignKey(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    title = models.CharField(max_length=160)
    goal = models.TextField()
    image_url = models.CharField(max_length=2048, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=AssistStatus.choices, default=AssistStatus.THINKING
    )
    history = models.JSONField(default=list)
    questions = models.JSONField(default=list)
    draft = models.JSONField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    thinking_since = models.DateTimeField(null=True, blank=True)
    run_id = models.UUIDField(null=True, blank=True)

    objects = TenantManager["AssistSession"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in AssistStatus]),
                name="assist_status_valid",
            )
        ]
        indexes = [
            models.Index(
                fields=["workspace", "connection", "-updated_at"], name="assist_recent_idx"
            )
        ]
        ordering = ["-updated_at"]


class AdLevel(models.TextChoices):
    CAMPAIGN = "campaign"
    AD_GROUP = "ad_group"
    AD = "ad"
    ASSET_GROUP = "asset_group"
    KEYWORD = "keyword"


class AdEntity(TenantModel):
    """Local copy of one campaign, ad group or ad, refreshed by sync and never fetched on read."""

    connection = models.ForeignKey(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    level = models.CharField(max_length=20, choices=AdLevel.choices)
    external_id = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    name = models.CharField(max_length=1000, blank=True, default="")
    status = models.CharField(max_length=20, blank=True, default="")
    review_status = models.CharField(max_length=20, blank=True, default="")
    data = models.JSONField(default=dict)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    missing_since = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["AdEntity"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "level", "external_id"], name="uniq_ad_entity"
            ),
            models.CheckConstraint(
                condition=Q(level__in=[v.value for v in AdLevel]), name="ad_entity_level_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["connection", "level", "parent"], name="ad_entity_tree_idx")
        ]


class AdMetricDaily(TenantModel):
    """Delivery numbers of one ad for one day (account timezone)."""

    entity = models.ForeignKey(AdEntity, on_delete=models.CASCADE, related_name="metrics")
    day = models.DateField()
    impressions = models.BigIntegerField(default=0)
    clicks = models.BigIntegerField(default=0)
    spend_micros = models.BigIntegerField(default=0)
    conversions = models.FloatField(default=0)
    conversion_value = models.FloatField(default=0)

    objects = TenantManager["AdMetricDaily"]()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["entity", "day"], name="uniq_ad_metric_day")]
        indexes = [models.Index(fields=["entity", "-day"], name="ad_metric_recent_idx")]


class AdChange(TenantModel):
    """A change noticed between two syncs (made here, in Ads Manager, or by the platform)."""

    entity = models.ForeignKey(AdEntity, on_delete=models.CASCADE, related_name="changes")
    at = models.DateTimeField()
    diff = models.JSONField(default=dict)

    objects = TenantManager["AdChange"]()

    class Meta:
        indexes = [models.Index(fields=["entity", "-at"], name="ad_change_recent_idx")]
        ordering = ["-at"]


class AdSyncState(TenantModel):
    connection = models.OneToOneField(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    counts = models.JSONField(default=dict)
    account = models.JSONField(default=dict)

    objects = TenantManager["AdSyncState"]()


class ReviewScope(models.TextChoices):
    AD = "ad"
    AD_GROUP = "ad_group"
    CAMPAIGN = "campaign"
    ACCOUNT = "account"


class AdReview(TenantModel):
    """One AI review of an ad (or of the whole account) with the state it looked at.

    The snapshot of the ad and its numbers are stored with the verdict so the next review can be
    compared to this one: what changed since, and whether the changes helped.
    """

    connection = models.ForeignKey(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    scope = models.CharField(max_length=10, choices=ReviewScope.choices)
    entity = models.ForeignKey(
        AdEntity, null=True, blank=True, on_delete=models.CASCADE, related_name="reviews"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=10, default="thinking")
    thinking_since = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    result = models.JSONField(null=True, blank=True)
    snapshot = models.JSONField(default=dict)
    previous = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    comparison = models.JSONField(null=True, blank=True)
    applied = models.JSONField(default=dict)

    objects = TenantManager["AdReview"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["thinking", "done", "failed"]),
                name="ad_review_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(scope__in=[v.value for v in ReviewScope]), name="ad_review_scope_valid"
            ),
        ]
        indexes = [
            models.Index(fields=["connection", "scope", "-created_at"], name="ad_review_idx")
        ]
        ordering = ["-created_at"]


class AdVariantSet(TenantModel):
    """Three AI-written alternatives for one ad, waiting for the owner's choice."""

    connection = models.ForeignKey(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    entity = models.ForeignKey(AdEntity, on_delete=models.CASCADE, related_name="variant_sets")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    instruction = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, default="thinking")
    thinking_since = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    variants = models.JSONField(default=list)
    chosen_index = models.PositiveSmallIntegerField(null=True, blank=True)
    action_id = models.UUIDField(null=True, blank=True)
    applied = models.BooleanField(default=False)

    objects = TenantManager["AdVariantSet"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["thinking", "done", "failed"]),
                name="ad_variant_status_valid",
            )
        ]
        indexes = [models.Index(fields=["entity", "-created_at"], name="ad_variant_recent_idx")]
        ordering = ["-created_at"]


class AdHintProposal(TenantModel):
    """An AI rewrite of one ad group's context hints, waiting for the owner to apply it."""

    connection = models.ForeignKey(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    entity = models.ForeignKey(AdEntity, on_delete=models.CASCADE, related_name="hint_proposals")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    instruction = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, default="thinking")
    thinking_since = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    before = models.JSONField(default=list)
    after = models.JSONField(default=list)
    summary = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    action_id = models.UUIDField(null=True, blank=True)
    applied = models.BooleanField(default=False)
    decided = models.BooleanField(default=False)

    objects = TenantManager["AdHintProposal"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["thinking", "done", "failed"]),
                name="ad_hint_status_valid",
            )
        ]
        indexes = [models.Index(fields=["entity", "-created_at"], name="ad_hint_recent_idx")]
        ordering = ["-created_at"]


class AdKeywordProposal(TenantModel):
    """AI suggestions for one Google Ads ad group's keywords, built from its real search terms."""

    connection = models.ForeignKey(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    entity = models.ForeignKey(AdEntity, on_delete=models.CASCADE, related_name="keyword_proposals")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    instruction = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, default="thinking")
    thinking_since = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    search_terms = models.JSONField(default=list)
    negatives = models.JSONField(default=list)
    additions = models.JSONField(default=list)
    pauses = models.JSONField(default=list)
    summary = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    applied = models.JSONField(default=dict)

    objects = TenantManager["AdKeywordProposal"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["thinking", "done", "failed"]),
                name="ad_keyword_status_valid",
            )
        ]
        indexes = [models.Index(fields=["entity", "-created_at"], name="ad_keyword_recent_idx")]
        ordering = ["-created_at"]


class AdAlert(TenantModel):
    """Something on an ad account that needs attention, found by the daily checks."""

    connection = models.ForeignKey(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    entity = models.ForeignKey(
        AdEntity, null=True, blank=True, on_delete=models.CASCADE, related_name="alerts"
    )
    code = models.CharField(max_length=60)
    severity = models.CharField(max_length=10)
    params = models.JSONField(default=dict)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    acknowledged = models.BooleanField(default=False)

    objects = TenantManager["AdAlert"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(severity__in=["high", "medium", "low"]), name="ad_alert_severity_valid"
            )
        ]
        indexes = [
            models.Index(
                fields=["connection", "resolved_at", "-last_seen_at"], name="ad_alert_open_idx"
            )
        ]
        ordering = ["-last_seen_at"]


class AdOptimization(TenantModel):
    """One AI optimisation run for a campaign: findings, a landing page audit and itemised changes.

    Nothing in ``plan`` changes the ad account. The owner picks items and applies them as actions
    (``applied`` records what was sent), so every change is approved by a person.
    """

    connection = models.ForeignKey(
        "connections.Connection", on_delete=models.CASCADE, related_name="+"
    )
    entity = models.ForeignKey(AdEntity, on_delete=models.CASCADE, related_name="optimizations")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    goal = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, default="thinking")
    thinking_since = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True, default="")
    landing = models.JSONField(default=list)
    plan = models.JSONField(null=True, blank=True)
    applied = models.JSONField(default=dict)

    objects = TenantManager["AdOptimization"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["thinking", "done", "failed"]),
                name="ad_optimization_status_valid",
            )
        ]
        indexes = [
            models.Index(fields=["entity", "-created_at"], name="ad_optimization_recent_idx")
        ]
        ordering = ["-created_at"]
