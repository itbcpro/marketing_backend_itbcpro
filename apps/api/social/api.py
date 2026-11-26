"""Instagram screens: setup, accounts, posts and reels, insights, reports and response settings."""

import uuid
from contextlib import suppress
from datetime import date, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count
from django.http import HttpRequest
from ninja import Query, Router, Schema

from connections.models import Connection, ConnectionStatus
from core.errors import DomainError, NotFoundError, PermissionDeniedError
from identity.authentication import api_auth
from identity.models import User
from social import analysis, analytics, images, response_settings, sync
from social.instagram import factory
from social.instagram.client import PROVIDER
from social.instagram.credentials import app_credentials
from social.instagram.provider import SCOPES
from social.models import (
    AccountInsight,
    CommentStatus,
    ReportScope,
    SocialAccount,
    SocialComment,
    SocialMedia,
    SocialReport,
)
from social.schemas import ReplySettings
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission, role_allows

router = Router(tags=["instagram"], auth=api_auth)

MAX_PAGE = 100
KINDS = {"image", "carousel", "video", "reel", "story"}
SORTS = {
    "newest": "-posted_at",
    "oldest": "posted_at",
}
METRIC_SORTS = {"reach", "views", "interactions", "saves", "shares", "engagement_rate"}


# --- shared helpers ---------------------------------------------------------------------------


def account_of(
    request: HttpRequest, account_id: uuid.UUID, permission: Permission = Permission.VIEW
) -> SocialAccount:
    membership = resolve_workspace(request, permission)
    found = (
        SocialAccount.objects.select_related("connection")
        .filter(pk=account_id, workspace_id=membership.workspace_id)
        .first()
    )
    if found is None:
        raise NotFoundError("Instagram account not found")
    return found


def user_of(request: HttpRequest) -> User:
    user: User = request.auth  # type: ignore[attr-defined]
    return user


# --- setup ------------------------------------------------------------------------------------


class IgSetupOut(Schema):
    configured: bool
    missing: list[str]
    redirect_uri: str
    webhook_url: str
    webhook_verify_token_set: bool
    public_url_ready: bool
    image_service_connected: bool
    scopes: list[str]


@router.get("/status", response=IgSetupOut, operation_id="instagram_status")
def setup_status(request: HttpRequest) -> IgSetupOut:
    """What the platform still needs before Instagram works (shown on the setup screen)."""
    membership = resolve_workspace(request, Permission.VIEW)
    missing = app_credentials(membership.workspace_id).missing()
    base = str(settings.PUBLIC_BASE_URL).rstrip("/")
    return IgSetupOut(
        configured=not missing,
        missing=missing,
        redirect_uri=f"{settings.WEB_BASE_URL}/bff/api/v1/connections/oauth/{PROVIDER}/callback",
        webhook_url=f"{base}/bff/api/v1/social/instagram/webhook",
        webhook_verify_token_set=bool(settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN),
        public_url_ready=base.startswith("https://"),
        image_service_connected=images.generator_for(membership.workspace_id) is not None,
        scopes=list(SCOPES),
    )


class IgAccountOut(Schema):
    id: uuid.UUID | None
    connection_id: uuid.UUID
    connection_status: str
    health_detail: str
    token_expires_at: datetime | None
    missing_scopes: list[str]
    username: str
    name: str
    account_type: str
    profile_picture_url: str
    biography: str
    website: str
    followers_count: int
    follows_count: int
    media_count: int
    last_synced_at: datetime | None
    last_error: str
    counts: dict[str, Any]
    webhooks_subscribed: bool


def _account_out(connection: Connection, account: SocialAccount | None) -> IgAccountOut:
    granted = set(connection.granted_scopes)
    return IgAccountOut(
        id=account.pk if account else None,
        connection_id=connection.pk,
        connection_status=connection.status,
        health_detail=connection.health_detail,
        token_expires_at=connection.expires_at,
        missing_scopes=[s for s in SCOPES if s not in granted],
        username=account.username if account else connection.display_name.lstrip("@"),
        name=account.name if account else "",
        account_type=account.account_type if account else "",
        profile_picture_url=account.profile_picture_url if account else "",
        biography=account.biography if account else "",
        website=account.website if account else "",
        followers_count=account.followers_count if account else 0,
        follows_count=account.follows_count if account else 0,
        media_count=account.media_count if account else 0,
        last_synced_at=account.last_synced_at if account else None,
        last_error=account.last_error if account else "",
        counts=account.counts if account else {},
        webhooks_subscribed=account.webhooks_subscribed if account else False,
    )


@router.get("/accounts", response=list[IgAccountOut], operation_id="instagram_accounts")
def list_accounts(request: HttpRequest) -> list[IgAccountOut]:
    membership = resolve_workspace(request, Permission.VIEW)
    connections = Connection.objects.filter(
        workspace_id=membership.workspace_id, provider=PROVIDER
    ).exclude(status=ConnectionStatus.REVOKED)
    accounts = {
        a.connection_id: a for a in SocialAccount.objects.filter(connection__in=connections)
    }
    return [_account_out(c, accounts.get(c.pk)) for c in connections.order_by("created_at")]


@router.post(
    "/connections/{connection_id}/sync", response=IgAccountOut, operation_id="instagram_sync"
)
def sync_now(request: HttpRequest, connection_id: uuid.UUID) -> IgAccountOut:
    """Refresh the local copy now (profile, posts, insights, comments, messages)."""
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    connection = (
        Connection.objects.filter(
            pk=connection_id, workspace_id=membership.workspace_id, provider=PROVIDER
        )
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )
    if connection is None:
        raise NotFoundError("Instagram connection not found")
    with suppress(DomainError):  # the error is stored on the account and shown on the screen
        sync.sync_connection(connection)
    return _account_out(connection, SocialAccount.objects.filter(connection=connection).first())


@router.get("/accounts/{account_id}", response=IgAccountOut, operation_id="instagram_account")
def get_account(request: HttpRequest, account_id: uuid.UUID) -> IgAccountOut:
    account = account_of(request, account_id)
    return _account_out(account.connection, account)


class IgQuotaOut(Schema):
    used: int
    total: int
    available: bool
    known: bool = True


@router.get(
    "/accounts/{account_id}/publishing-limit", response=IgQuotaOut, operation_id="instagram_quota"
)
def publishing_limit(request: HttpRequest, account_id: uuid.UUID) -> IgQuotaOut:
    """Posts used out of the rolling 24 hour publishing limit, read live from Instagram."""
    account = account_of(request, account_id)
    try:
        with factory.open_client(account.connection) as client:
            limit = client.publishing_limit()
    except DomainError:
        # A widget on the overview: Instagram being unreachable must not break the screen.
        return IgQuotaOut(used=0, total=0, available=True, known=False)
    total = int((limit.get("config") or {}).get("quota_total") or 0)
    used = int(limit.get("quota_usage") or 0)
    return IgQuotaOut(used=used, total=total, available=total == 0 or used < total)


# --- posts and reels --------------------------------------------------------------------------


class IgMediaOut(Schema):
    id: uuid.UUID
    external_id: str
    kind: str
    caption: str
    permalink: str
    media_url: str
    thumbnail_url: str
    posted_at: datetime
    like_count: int
    comments_count: int
    comments_enabled: bool
    metrics: dict[str, Any]
    metrics_synced_at: datetime | None
    analysis: dict[str, Any] | None
    analysis_at: datetime | None
    unanswered_comments: int
    removed: bool


class IgMediaPage(Schema):
    items: list[IgMediaOut]
    total: int


def _media_out(media: SocialMedia, unanswered: int = 0) -> IgMediaOut:
    return IgMediaOut(
        id=media.pk,
        external_id=media.external_id,
        kind=media.kind,
        caption=media.caption,
        permalink=media.permalink,
        media_url=media.media_url,
        thumbnail_url=media.thumbnail_url,
        posted_at=media.posted_at,
        like_count=media.like_count,
        comments_count=media.comments_count,
        comments_enabled=media.comments_enabled,
        metrics=media.metrics,
        metrics_synced_at=media.metrics_synced_at,
        analysis=media.analysis,
        analysis_at=media.analysis_at,
        unanswered_comments=unanswered,
        removed=media.removed_at is not None,
    )


class IgMediaFilters(Schema):
    kind: str = ""
    sort: str = "newest"
    q: str = ""
    limit: int = 24
    offset: int = 0


def _metric_value(media: SocialMedia, key: str) -> float:
    value = (media.metrics or {}).get(key)
    return float(value) if isinstance(value, int | float) else -1.0


@router.get(
    "/accounts/{account_id}/media", response=IgMediaPage, operation_id="instagram_media_list"
)
def list_media(
    request: HttpRequest, account_id: uuid.UUID, filters: Query[IgMediaFilters]
) -> IgMediaPage:
    """Posts, reels, videos and stories with their metrics; sort by date or by any metric."""
    account = account_of(request, account_id)
    rows = SocialMedia.objects.filter(account=account, removed_at__isnull=True)
    if filters.kind in KINDS:
        rows = rows.filter(kind=filters.kind)
    if filters.q:
        rows = rows.filter(caption__icontains=filters.q.strip()[:100])
    limit = max(1, min(filters.limit, MAX_PAGE))
    offset = max(0, filters.offset)
    total = rows.count()
    if filters.sort in METRIC_SORTS:
        ordered = sorted(rows, key=lambda m: _metric_value(m, filters.sort), reverse=True)
        page = ordered[offset : offset + limit]
    else:
        page = list(rows.order_by(SORTS.get(filters.sort, "-posted_at"))[offset : offset + limit])
    unanswered = {
        row["media_id"]: row["n"]
        for row in SocialComment.objects.filter(
            media__in=page,
            from_owner=False,
            parent_external_id="",
            status__in=[CommentStatus.NEW, CommentStatus.DRAFT_READY, CommentStatus.ESCALATED],
        )
        .values("media_id")
        .annotate(n=Count("id"))
    }
    return IgMediaPage(items=[_media_out(m, unanswered.get(m.pk, 0)) for m in page], total=total)


class IgSnapshotOut(Schema):
    day: date
    age_hours: int
    metrics: dict[str, Any]


class IgMediaDetailOut(Schema):
    media: IgMediaOut
    children: list[dict[str, Any]]
    snapshots: list[IgSnapshotOut]
    baseline: dict[str, Any]


def _media_of(request: HttpRequest, media_id: uuid.UUID, permission: Permission) -> SocialMedia:
    membership = resolve_workspace(request, permission)
    found = (
        SocialMedia.objects.select_related("account__connection")
        .filter(pk=media_id, workspace_id=membership.workspace_id)
        .first()
    )
    if found is None:
        raise NotFoundError("post not found")
    return found


@router.get("/media/{media_id}", response=IgMediaDetailOut, operation_id="instagram_media_get")
def get_media(request: HttpRequest, media_id: uuid.UUID) -> IgMediaDetailOut:
    media = _media_of(request, media_id, Permission.VIEW)
    return IgMediaDetailOut(
        media=_media_out(media),
        children=media.children,
        snapshots=[
            IgSnapshotOut(day=s.day, age_hours=s.age_hours, metrics=s.metrics)
            for s in media.snapshots.order_by("day")
        ],
        baseline=analytics.baseline_for(media),
    )


# --- insights and statistics ------------------------------------------------------------------


class IgInsightRowOut(Schema):
    day: date
    followers_count: int
    totals: dict[str, Any]
    series: dict[str, Any]


@router.get(
    "/accounts/{account_id}/insights",
    response=list[IgInsightRowOut],
    operation_id="instagram_insights",
)
def insights(request: HttpRequest, account_id: uuid.UUID, days: int = 90) -> list[IgInsightRowOut]:
    """Daily account snapshots (28 day totals, reach and follower series), oldest first."""
    account = account_of(request, account_id)
    since = (datetime.now().date()) - timedelta(days=max(7, min(days, 365)))  # noqa: DTZ005
    rows = AccountInsight.objects.filter(account=account, day__gte=since).order_by("day")
    return [
        IgInsightRowOut(
            day=r.day, followers_count=r.followers_count, totals=r.totals, series=r.series
        )
        for r in rows
    ]


@router.get("/accounts/{account_id}/stats", response=dict[str, Any], operation_id="instagram_stats")
def stats(request: HttpRequest, account_id: uuid.UUID, days: int = 90) -> dict[str, Any]:
    """The measured statistics the AI reads: formats, timing, best and weakest posts, audience."""
    account = account_of(request, account_id)
    return analytics.compute_stats(account, max(7, min(days, 365)))


# --- reports ----------------------------------------------------------------------------------


class IgReportIn(Schema):
    scope: str = "account"
    instruction: str = ""
    period_days: int = 90


class IgReportOut(Schema):
    id: uuid.UUID
    scope: str
    media_id: uuid.UUID | None
    status: str
    error_code: str
    thinking_since: datetime | None
    period_days: int
    instruction: str
    result: dict[str, Any] | None
    stats: dict[str, Any]
    created_at: datetime


class IgReportSummaryOut(Schema):
    id: uuid.UUID
    scope: str
    status: str
    verdict: str
    score: int | None
    created_at: datetime


def report_out(report: SocialReport) -> IgReportOut:
    return IgReportOut(
        id=report.pk,
        scope=report.scope,
        media_id=report.media_id,
        status=report.status,
        error_code=report.error_code,
        thinking_since=report.thinking_since,
        period_days=report.period_days,
        instruction=report.instruction,
        result=report.result,
        stats=report.stats if report.scope != ReportScope.PLAN else {},
        created_at=report.created_at,
    )


@router.get(
    "/accounts/{account_id}/reports",
    response=list[IgReportSummaryOut],
    operation_id="instagram_reports",
)
def list_reports(
    request: HttpRequest, account_id: uuid.UUID, scope: str = "account"
) -> list[IgReportSummaryOut]:
    account = account_of(request, account_id)
    rows = SocialReport.objects.filter(account=account, scope=scope, media__isnull=True)[:20]
    return [
        IgReportSummaryOut(
            id=r.pk,
            scope=r.scope,
            status=r.status,
            verdict=str((r.result or {}).get("verdict", "")),
            score=(r.result or {}).get("score"),
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post(
    "/accounts/{account_id}/reports", response=IgReportOut, operation_id="instagram_report_start"
)
def start_report(request: HttpRequest, account_id: uuid.UUID, body: IgReportIn) -> IgReportOut:
    """Ask the AI to analyse the whole account or to plan the next four weeks."""
    account = account_of(request, account_id, Permission.EDIT_CONTENT)
    scope = ReportScope.PLAN if body.scope == "plan" else ReportScope.ACCOUNT
    return report_out(
        analysis.start(
            account,
            user_of(request).pk,
            scope,
            instruction=body.instruction,
            period_days=body.period_days,
        )
    )


@router.get("/reports/{report_id}", response=IgReportOut, operation_id="instagram_report_get")
def get_report(request: HttpRequest, report_id: uuid.UUID) -> IgReportOut:
    membership = resolve_workspace(request, Permission.VIEW)
    return report_out(analysis.get(membership.workspace_id, report_id))


@router.post(
    "/media/{media_id}/analysis", response=IgReportOut, operation_id="instagram_media_analysis"
)
def analyse_media(request: HttpRequest, media_id: uuid.UUID, body: IgReportIn) -> IgReportOut:
    """Ask the AI how one post or reel performed against the account's own baseline."""
    media = _media_of(request, media_id, Permission.EDIT_CONTENT)
    return report_out(
        analysis.start(
            media.account,
            user_of(request).pk,
            ReportScope.MEDIA,
            media=media,
            instruction=body.instruction,
            period_days=body.period_days,
        )
    )


class IgPlanAppliedOut(Schema):
    draft_ids: list[uuid.UUID]


@router.post(
    "/reports/{report_id}/apply-plan",
    response=IgPlanAppliedOut,
    operation_id="instagram_plan_apply",
)
def apply_plan(request: HttpRequest, report_id: uuid.UUID) -> IgPlanAppliedOut:
    """Put every item of a content plan on the calendar as an idea."""
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    report = analysis.get(membership.workspace_id, report_id)
    drafts = analysis.apply_plan(report, user_of(request).pk)
    return IgPlanAppliedOut(draft_ids=[d.pk for d in drafts])


# --- response settings ------------------------------------------------------------------------


@router.get(
    "/accounts/{account_id}/settings", response=ReplySettings, operation_id="instagram_settings"
)
def get_settings(request: HttpRequest, account_id: uuid.UUID) -> ReplySettings:
    return response_settings.load(account_of(request, account_id))


@router.put(
    "/accounts/{account_id}/settings",
    response=ReplySettings,
    operation_id="instagram_settings_save",
)
def save_settings(
    request: HttpRequest, account_id: uuid.UUID, body: ReplySettings
) -> ReplySettings:
    """Save how comments and messages are handled. Allowing "auto" needs the autonomy right."""
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    account = account_of(request, account_id, Permission.EDIT_CONTENT)
    previous = response_settings.load(account)
    if response_settings.needs_autonomy(body, previous) and not role_allows(
        membership.role, Permission.CHANGE_AUTONOMY
    ):
        raise PermissionDeniedError()
    return response_settings.save(account, body, user_of(request))


@router.post(
    "/accounts/{account_id}/settings/disable-automation",
    response=ReplySettings,
    operation_id="instagram_settings_disable_automation",
)
def disable_automation(request: HttpRequest, account_id: uuid.UUID) -> ReplySettings:
    """Panic button: everything that was automatic becomes a draft for a person."""
    account = account_of(request, account_id, Permission.EDIT_CONTENT)
    response_settings.disable_all_automation(account, user_of(request))
    return response_settings.load(account)
