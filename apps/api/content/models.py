"""Websites the platform watches and improves: a WordPress site or a site built from a Git repo."""

from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TenantManager, TenantModel


class WebsiteKind(models.TextChoices):
    WORDPRESS = "wordpress"
    GIT_SITE = "git_site"
    """A site generated from a repository that a push builds and deploys."""


class WebsiteStatus(models.TextChoices):
    PENDING = "pending"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"


class Website(TenantModel):
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=WebsiteKind.choices)
    base_url = models.CharField(max_length=500)
    connection = models.ForeignKey(
        "connections.Connection",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    repo = models.CharField(max_length=200, blank=True, default="")
    """``owner/name`` of the repository for a Git-built site (read-only metadata for now)."""
    branch = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=WebsiteStatus.choices, default=WebsiteStatus.PENDING
    )
    status_detail = models.CharField(max_length=500, blank=True, default="")
    profile = models.JSONField(default=dict)
    """Result of the last probe: platform facts, detected plugins, counts, warnings."""
    probed_at = models.DateTimeField(null=True, blank=True)
    brief = models.JSONField(default=dict)
    """Site-wide defaults for AI reviews: country, region, audience and goal."""

    objects = TenantManager["Website"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "base_url"], name="uniq_website_url"),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in WebsiteKind]), name="website_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in WebsiteStatus]),
                name="website_status_valid",
            ),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ItemSource(models.TextChoices):
    WORDPRESS = "wordpress"
    SITEMAP = "sitemap"


class ContentItem(TenantModel):
    """One public page of a website: a WordPress post or page, or a URL found in the sitemap.

    The inventory is a local copy that audits and agents read; it never writes back to the site.
    """

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="items")
    key = models.CharField(max_length=600)
    """Stable identity: ``wp:<type>:<id>`` for WordPress, ``url:<address>`` for sitemap pages."""
    source = models.CharField(max_length=20, choices=ItemSource.choices)
    kind = models.CharField(max_length=60)
    """``post``, ``page`` or a custom post type for WordPress; ``page`` for sitemap URLs."""
    external_id = models.CharField(max_length=40, blank=True, default="")
    url = models.CharField(max_length=1000, blank=True, default="")
    slug = models.CharField(max_length=300, blank=True, default="")
    title = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(max_length=30, default="publish")
    language = models.CharField(max_length=20, blank=True, default="")
    author = models.CharField(max_length=200, blank=True, default="")
    terms = models.JSONField(default=dict)
    """Category and tag names, ``{"categories": [...], "tags": [...]}``."""
    published_at = models.DateTimeField(null=True, blank=True)
    modified_at = models.DateTimeField(null=True, blank=True)
    seo = models.JSONField(default=dict)
    """Title, description, canonical and robots as the SEO plugin or the page itself states them."""
    text = models.TextField(blank=True, default="")
    outline = models.JSONField(default=list)
    """Headings in order: ``[{"level": 2, "text": "..."}]``."""
    images = models.JSONField(default=list)
    """Images of the page: address, alt text, media library id (if any) and nearby text."""
    stats = models.JSONField(default=dict)
    """word_count, internal_links, external_links, images, images_missing_alt."""
    content_hash = models.CharField(max_length=64, blank=True, default="")
    in_sitemap = models.BooleanField(default=False)
    http_status = models.IntegerField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    changed_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    brief = models.JSONField(default=dict)
    """This page's brief: primary and secondary keywords and any site default it overrides."""

    objects = TenantManager["ContentItem"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["website", "key"], name="uniq_content_item_key"),
        ]
        indexes = [
            models.Index(fields=["website", "kind", "status"], name="content_item_kind_idx"),
            models.Index(fields=["website", "removed_at"], name="content_item_removed_idx"),
        ]
        ordering = ["-modified_at", "title"]

    def __str__(self) -> str:
        return self.title or self.url


class SyncStatus(models.TextChoices):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SyncRun(TenantModel):
    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="sync_runs")
    kind = models.CharField(max_length=12, default="inventory")
    """``inventory`` (pages) or ``search`` (Search Console data)."""
    status = models.CharField(max_length=10, choices=SyncStatus.choices, default=SyncStatus.QUEUED)
    full = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    stats = models.JSONField(default=dict)
    error = models.CharField(max_length=500, blank=True, default="")

    objects = TenantManager["SyncRun"]()

    class Meta:
        ordering = ["-created_at"]

    QUEUE_PATIENCE_SECONDS = 120
    RUN_PATIENCE_SECONDS = 2700

    @property
    def is_stale(self) -> bool:
        """Waiting far longer than normal: the background worker is probably not running."""
        if self.status == SyncStatus.QUEUED:
            return (timezone.now() - self.created_at).total_seconds() > self.QUEUE_PATIENCE_SECONDS
        if self.status == SyncStatus.RUNNING and self.started_at:
            return (timezone.now() - self.started_at).total_seconds() > self.RUN_PATIENCE_SECONDS
        return False


class AnalysisStatus(models.TextChoices):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PageAnalysis(TenantModel):
    """One AI review of one page. ``content_hash`` shows whether the page changed since."""

    item = models.ForeignKey(ContentItem, on_delete=models.CASCADE, related_name="analyses")
    status = models.CharField(
        max_length=10, choices=AnalysisStatus.choices, default=AnalysisStatus.QUEUED
    )
    content_hash = models.CharField(max_length=64, blank=True, default="")
    instruction = models.CharField(max_length=500, blank=True, default="")
    result = models.JSONField(default=dict)
    error = models.CharField(max_length=300, blank=True, default="")
    requested_by = models.CharField(max_length=100, blank=True, default="")
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["PageAnalysis"]()

    class Meta:
        ordering = ["-created_at"]


class SuggestionStatus(models.TextChoices):
    PROPOSED = "proposed"
    APPLIED = "applied"
    DISMISSED = "dismissed"


class PageSuggestion(TenantModel):
    """A concrete AI suggestion: a field change that can be applied, or advice to act on."""

    item = models.ForeignKey(ContentItem, on_delete=models.CASCADE, related_name="suggestions")
    analysis = models.ForeignKey(PageAnalysis, on_delete=models.CASCADE, related_name="suggestions")
    field = models.CharField(max_length=30)
    """``seo_title``, ``meta_description``, ``title``, ``slug``, ``excerpt`` or ``advice``."""
    current = models.TextField(blank=True, default="")
    proposed = models.TextField(blank=True, default="")
    reason = models.TextField(blank=True, default="")
    impact = models.CharField(max_length=10, default="medium")
    status = models.CharField(
        max_length=10, choices=SuggestionStatus.choices, default=SuggestionStatus.PROPOSED
    )

    objects = TenantManager["PageSuggestion"]()

    class Meta:
        ordering = ["created_at"]


class SearchProperty(TenantModel):
    """The Search Console property a website is linked to, and how far its data has been copied."""

    website = models.OneToOneField(Website, on_delete=models.CASCADE, related_name="search")
    connection = models.ForeignKey(
        "connections.Connection", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    site_url = models.CharField(max_length=500)
    """``sc-domain:example.com`` or a URL prefix such as ``https://example.com/``."""
    permission = models.CharField(max_length=40, blank=True, default="")
    first_date = models.DateField(null=True, blank=True)
    last_date = models.DateField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")

    objects = TenantManager["SearchProperty"]()


class GscDaily(TenantModel):
    """Totals for the whole site per day."""

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="+")
    date = models.DateField()
    clicks = models.IntegerField(default=0)
    impressions = models.IntegerField(default=0)
    position = models.FloatField(default=0.0)

    objects = TenantManager["GscDaily"]()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["website", "date"], name="uniq_gsc_daily")]


class GscPageDaily(TenantModel):
    """One page's search numbers per day."""

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="+")
    date = models.DateField()
    page = models.CharField(max_length=1000)
    clicks = models.IntegerField(default=0)
    impressions = models.IntegerField(default=0)
    position = models.FloatField(default=0.0)

    objects = TenantManager["GscPageDaily"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["website", "date", "page"], name="uniq_gsc_page_daily")
        ]
        indexes = [models.Index(fields=["website", "page", "date"], name="gsc_page_idx")]


class GscQueryDaily(TenantModel):
    """One search query that showed one page, per day."""

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="+")
    date = models.DateField()
    query = models.CharField(max_length=500)
    page = models.CharField(max_length=1000)
    clicks = models.IntegerField(default=0)
    impressions = models.IntegerField(default=0)
    position = models.FloatField(default=0.0)

    objects = TenantManager["GscQueryDaily"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["website", "date", "query", "page"], name="uniq_gsc_query_daily"
            )
        ]
        indexes = [
            models.Index(fields=["website", "date"], name="gsc_query_date_idx"),
            models.Index(fields=["website", "query"], name="gsc_query_idx"),
            models.Index(fields=["website", "page", "date"], name="gsc_query_page_idx"),
        ]


class GscSitemap(TenantModel):
    """A sitemap as Search Console sees it."""

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="gsc_sitemaps")
    path = models.CharField(max_length=1000)
    is_index = models.BooleanField(default=False)
    is_pending = models.BooleanField(default=False)
    last_submitted = models.DateTimeField(null=True, blank=True)
    last_downloaded = models.DateTimeField(null=True, blank=True)
    errors = models.IntegerField(default=0)
    warnings = models.IntegerField(default=0)
    contents = models.JSONField(default=list)
    """Submitted and indexed counts per type, as Search Console reports them."""

    objects = TenantManager["GscSitemap"]()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["website", "path"], name="uniq_gsc_sitemap")]


class SearchAnalysis(TenantModel):
    """One AI analysis of a website's search results for a period, compared with another."""

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="search_analyses")
    status = models.CharField(
        max_length=10, choices=AnalysisStatus.choices, default=AnalysisStatus.QUEUED
    )
    period_start = models.DateField()
    period_end = models.DateField()
    compare = models.CharField(max_length=10, default="previous")
    instruction = models.CharField(max_length=500, blank=True, default="")
    result = models.JSONField(default=dict)
    error = models.CharField(max_length=300, blank=True, default="")
    requested_by = models.CharField(max_length=100, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["SearchAnalysis"]()

    class Meta:
        ordering = ["-created_at"]


class PageSpeedResult(TenantModel):
    """One Google PageSpeed Insights run for one address and device."""

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="+")
    url = models.CharField(max_length=1000)
    strategy = models.CharField(max_length=10)
    """``mobile`` or ``desktop``."""
    status = models.CharField(
        max_length=10, choices=AnalysisStatus.choices, default=AnalysisStatus.QUEUED
    )
    performance = models.SmallIntegerField(null=True, blank=True)
    seo = models.SmallIntegerField(null=True, blank=True)
    accessibility = models.SmallIntegerField(null=True, blank=True)
    best_practices = models.SmallIntegerField(null=True, blank=True)
    lab = models.JSONField(default=dict)
    """Lab metrics measured now: lcp_ms, fcp_ms, cls, tbt_ms, speed_index_ms."""
    field = models.JSONField(default=dict)
    """Real-user Chrome numbers when Google has enough visits: lcp_ms, inp_ms, cls, category."""
    opportunities = models.JSONField(default=list)
    error = models.CharField(max_length=300, blank=True, default="")
    requested_by = models.CharField(max_length=100, blank=True, default="")
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["PageSpeedResult"]()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["website", "url", "strategy"], name="psi_lookup_idx")]


class IdeaStatus(models.TextChoices):
    SUGGESTED = "suggested"
    WRITING = "writing"
    WRITTEN = "written"
    DISMISSED = "dismissed"


class ArticleIdea(TenantModel):
    """A suggested blog topic. The owner (or an automation) picks one and a blog type."""

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="ideas")
    batch = models.UUIDField(null=True, blank=True, db_index=True)
    """Ideas suggested together share a batch id."""
    title = models.CharField(max_length=300)
    primary_keyword = models.CharField(max_length=160)
    secondary_keywords = models.JSONField(default=list)
    intent = models.CharField(max_length=30, blank=True, default="")
    blog_type = models.CharField(max_length=30)
    angle = models.TextField(blank=True, default="")
    why_now = models.TextField(blank=True, default="")
    evidence = models.TextField(blank=True, default="")
    cluster = models.CharField(max_length=160, blank=True, default="")
    priority = models.CharField(max_length=10, default="medium")
    overlaps = models.JSONField(default=list)
    """Addresses of existing posts that cover similar ground."""
    status = models.CharField(
        max_length=12, choices=IdeaStatus.choices, default=IdeaStatus.SUGGESTED
    )
    requested_by = models.CharField(max_length=100, blank=True, default="")

    objects = TenantManager["ArticleIdea"]()

    class Meta:
        ordering = ["-created_at", "title"]
        indexes = [models.Index(fields=["website", "status"], name="idea_site_status_idx")]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in IdeaStatus]), name="idea_status_valid"
            ),
        ]

    def __str__(self) -> str:
        return self.title


class ArticleStatus(models.TextChoices):
    QUEUED = "queued"
    WRITING = "writing"
    READY = "ready"
    FAILED = "failed"


class Article(TenantModel):
    """One long article, written in persisted steps so it survives restarts."""

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="articles")
    idea = models.ForeignKey(
        ArticleIdea, null=True, blank=True, on_delete=models.SET_NULL, related_name="articles"
    )
    title = models.CharField(max_length=300)
    primary_keyword = models.CharField(max_length=160)
    secondary_keywords = models.JSONField(default=list)
    blog_type = models.CharField(max_length=30)
    min_words = models.PositiveIntegerField(default=8000)
    instruction = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=10, choices=ArticleStatus.choices, default=ArticleStatus.QUEUED
    )
    step = models.CharField(max_length=40, blank=True, default="")
    """``plan``, ``section:<n>``, ``faq``, ``audit`` or ``done``; the next piece of work."""
    plan = models.JSONField(default=dict)
    sections = models.JSONField(default=list)
    """``[{"heading", "markdown", "summary", "words", "open_claims"}]`` in order."""
    faq = models.JSONField(default=list)
    meta = models.JSONField(default=dict)
    """h1, seo_title, meta_description, slug."""
    audit = models.JSONField(default=dict)
    gate = models.JSONField(default=dict)
    """Measured checks: word count, headings, links, FAQ count, metadata lengths."""
    score = models.SmallIntegerField(null=True, blank=True)
    word_count = models.PositiveIntegerField(default=0)
    with_images = models.BooleanField(default=True)
    images = models.JSONField(default=list)
    """One entry per planned image: placement, alt, status, media asset and WordPress ids."""
    wp_post_id = models.PositiveBigIntegerField(null=True, blank=True)
    wp_url = models.CharField(max_length=1000, blank=True, default="")
    wp_status = models.CharField(max_length=20, blank=True, default="")
    """The WordPress post status after the last publish action: draft, future, publish."""
    error = models.CharField(max_length=300, blank=True, default="")
    requested_by = models.CharField(max_length=100, blank=True, default="")
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["Article"]()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["website", "status"], name="article_site_status_idx")]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in ArticleStatus]),
                name="article_status_valid",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class RevisionStatus(models.TextChoices):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    APPLIED = "applied"
    DISMISSED = "dismissed"


class PostRevision(TenantModel):
    """An AI rewrite of one post's body, kept until the owner applies or discards it."""

    item = models.ForeignKey(ContentItem, on_delete=models.CASCADE, related_name="revisions")
    status = models.CharField(
        max_length=10, choices=RevisionStatus.choices, default=RevisionStatus.QUEUED
    )
    instruction = models.CharField(max_length=500, blank=True, default="")
    focus = models.JSONField(default=list)
    """Check keys the owner wants fixed (for example ``headings``, ``internal_links``)."""
    before_html = models.TextField(blank=True, default="")
    after_html = models.TextField(blank=True, default="")
    base_modified_at = models.CharField(max_length=40, blank=True, default="")
    """The post's last-modified time (UTC, ISO) when the original was read."""
    changes = models.JSONField(default=list)
    notes = models.TextField(blank=True, default="")
    stats = models.JSONField(default=dict)
    error = models.CharField(max_length=300, blank=True, default="")
    requested_by = models.CharField(max_length=100, blank=True, default="")
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager["PostRevision"]()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["item", "status"], name="revision_item_status_idx")]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in RevisionStatus]),
                name="revision_status_valid",
            ),
        ]


class IgnoredCheck(TenantModel):
    """A fix the owner chose to set aside: one check on one page, or (``check_key`` empty) the page.

    It points at the page by ``item_key`` rather than a row id, so a full re-sync, or a page that
    disappears and comes back, keeps the choice. Only restoring it removes the row.
    """

    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name="ignored_checks")
    item_key = models.CharField(max_length=600)
    check_key = models.CharField(max_length=40, blank=True, default="")

    objects = TenantManager["IgnoredCheck"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["website", "item_key", "check_key"], name="uniq_ignored_check"
            ),
        ]
        ordering = ["-created_at"]
