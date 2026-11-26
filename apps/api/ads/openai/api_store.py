"""OpenAI Ads from the local copy: tree, sync, single-ad dashboard and AI reviews."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from ads import alerts, providers
from ads.models import (
    AdAlert,
    AdChange,
    AdEntity,
    AdLevel,
    AdReview,
    AdSyncState,
    ReviewScope,
)
from ads.openai import review as reviews
from ads.openai import rows as row_builder
from ads.openai import stats
from connections.models import Connection, ConnectionStatus
from core.errors import NotFoundError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["ads-openai-store"], auth=api_auth)

MICROS = 1_000_000
MAX_DAYS = 365
CHANGES_SHOWN = 50
REVIEWS_SHOWN = 10


class SyncOut(Schema):
    synced_at: datetime | None
    last_error: str
    counts: dict[str, Any]
    currency: str
    account: dict[str, Any]


class TreeOut(Schema):
    sync: SyncOut
    campaigns: list[dict[str, Any]]


class DayOut(Schema):
    day: str
    impressions: int
    clicks: int
    spend: float


class ReviewSummaryOut(Schema):
    id: uuid.UUID
    status: str
    verdict: str | None
    score: int | None
    created_at: datetime


class ReviewOut(Schema):
    id: uuid.UUID
    scope: str
    entity_id: uuid.UUID | None
    status: str
    error_code: str
    thinking_since: datetime | None
    created_at: datetime
    result: dict[str, Any] | None
    comparison: dict[str, Any] | None
    previous_id: uuid.UUID | None
    applied: dict[str, Any]


class StoredChangeOut(Schema):
    at: datetime
    level: str
    name: str
    diff: dict[str, Any]


class AdDetailOut(Schema):
    entity_id: uuid.UUID
    level: str
    row: dict[str, Any]
    children: list[dict[str, Any]]
    ads: list[dict[str, Any]]
    connection_id: uuid.UUID
    provider: str
    external_id: str
    name: str
    status: str
    review_status: str
    data: dict[str, Any]
    campaign: dict[str, Any] | None
    ad_group: dict[str, Any] | None
    sync: SyncOut
    days: list[DayOut]
    totals: dict[str, dict[str, float]]
    changes: list[StoredChangeOut]
    reviews: list[ReviewSummaryOut]
    platform: dict[str, Any]
    checks: list[dict[str, Any]]


def _connection(
    request: HttpRequest, connection_id: uuid.UUID, permission: Permission
) -> Connection:
    membership = resolve_workspace(request, permission)
    connection = (
        Connection.objects.filter(
            pk=connection_id,
            workspace_id=membership.workspace_id,
            provider__in=list(providers.PROVIDERS),
        )
        .exclude(status=ConnectionStatus.REVOKED)
        .first()
    )
    if connection is None:
        raise NotFoundError("OpenAI Ads connection not found")
    return connection


def _entity(request: HttpRequest, entity_id: uuid.UUID, permission: Permission) -> AdEntity:
    membership = resolve_workspace(request, permission)
    entity = (
        AdEntity.objects.filter(pk=entity_id, workspace_id=membership.workspace_id)
        .select_related("connection", "parent__parent")
        .first()
    )
    if entity is None:
        raise NotFoundError("ad not found")
    return entity


def _grandparent(entity: AdEntity, by_pk: dict[uuid.UUID, AdEntity]) -> uuid.UUID | None:
    parent = by_pk.get(entity.parent_id) if entity.parent_id is not None else None
    return parent.parent_id if parent else None


def _sync_out(connection: Connection) -> SyncOut:
    state = AdSyncState.objects.filter(connection=connection).first()
    account = state.account if state else {}
    return SyncOut(
        synced_at=state.last_synced_at if state else None,
        last_error=state.last_error if state else "",
        counts=state.counts if state else {},
        currency=str(account.get("currency_code", "")),
        account=account,
    )


def _node(entity: AdEntity) -> dict[str, Any]:
    return {**entity.data, "entity_id": str(entity.pk), "name": entity.name}


def _review_summary(review: AdReview) -> ReviewSummaryOut:
    result = review.result or {}
    return ReviewSummaryOut(
        id=review.pk,
        status=review.status,
        verdict=result.get("verdict"),
        score=result.get("score"),
        created_at=review.created_at,
    )


def _review_out(review: AdReview) -> ReviewOut:
    return ReviewOut(
        id=review.pk,
        scope=review.scope,
        entity_id=review.entity_id,
        status=review.status,
        error_code=review.error_code,
        thinking_since=review.thinking_since,
        created_at=review.created_at,
        result=review.result,
        comparison=review.comparison,
        previous_id=review.previous_id,
        applied=review.applied,
    )


@router.get("/store/tree", response=TreeOut, operation_id="openai_ads_store_tree")
def tree(request: HttpRequest, connection_id: uuid.UUID) -> TreeOut:
    """Campaigns, ad groups and ads from the local copy. Never calls the platform."""
    connection = _connection(request, connection_id, Permission.VIEW)
    entities = list(
        AdEntity.objects.filter(connection=connection, missing_since__isnull=True).order_by(
            "first_seen_at"
        )
    )
    rows = row_builder.build_rows(entities)
    groups: dict[uuid.UUID, list[dict[str, Any]]] = {}
    ads: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for e in entities:
        if e.level == AdLevel.AD and e.parent_id:
            ads.setdefault(e.parent_id, []).append({**_node(e), "row": rows[e.pk]})
    for e in entities:
        if e.level == AdLevel.AD_GROUP and e.parent_id:
            groups.setdefault(e.parent_id, []).append(
                {**_node(e), "row": rows[e.pk], "ads": ads.get(e.pk, [])}
            )
    campaigns = [
        {**_node(e), "row": rows[e.pk], "ad_groups": groups.get(e.pk, [])}
        for e in entities
        if e.level == AdLevel.CAMPAIGN
    ]
    return TreeOut(sync=_sync_out(connection), campaigns=campaigns)


@router.post("/store/sync", response=SyncOut, operation_id="openai_ads_store_sync")
def run_sync(request: HttpRequest, connection_id: uuid.UUID) -> SyncOut:
    """Fetch the account from the platform once and refresh the local copy."""
    connection = _connection(request, connection_id, Permission.EDIT_CONTENT)
    providers.for_connection(connection).sync(connection)
    alerts.evaluate(connection)
    return _sync_out(connection)


@router.get(
    "/store/entities/{entity_id}", response=AdDetailOut, operation_id="openai_ads_store_entity"
)
def entity_detail(request: HttpRequest, entity_id: uuid.UUID, days: int = 30) -> AdDetailOut:
    entity = _entity(request, entity_id, Permission.VIEW)
    if not 1 <= days <= MAX_DAYS:
        raise ValidationFailedError("days must be between 1 and 365")
    today = timezone.now().date()
    since = today - timedelta(days=days)
    state = AdSyncState.objects.filter(connection=entity.connection_id).first()
    account = state.account if state else {}
    provider = providers.for_entity(entity)
    ids = stats.ad_ids(entity)
    everything = list(
        AdEntity.objects.filter(connection=entity.connection_id, missing_since__isnull=True)
    )
    built = row_builder.build_rows(everything)
    group = entity if entity.level == AdLevel.AD_GROUP else entity.parent
    if entity.level == AdLevel.CAMPAIGN:
        group = None
    campaign = entity if entity.level == AdLevel.CAMPAIGN else (group.parent if group else None)
    kids = [built[e.pk] for e in everything if e.parent_id == entity.pk]
    kids.sort(key=lambda r: -r["spend_7d"])
    by_pk = {e.pk: e for e in everything}
    beneath = [
        built[e.pk]
        for e in everything
        if e.level == AdLevel.AD
        and entity.level != AdLevel.AD
        and entity.pk in {e.parent_id, _grandparent(e, by_pk)}
    ]
    beneath.sort(key=lambda r: -r["spend_7d"])
    related = {
        e.pk for e in everything if entity.pk in {e.pk, e.parent_id, _grandparent(e, by_pk)}
    } | {e.pk for e in (group, campaign) if e}
    changes = (
        AdChange.objects.filter(entity_id__in=list(related))
        .select_related("entity")
        .order_by("-at")[:CHANGES_SHOWN]
    )
    recent = AdReview.objects.filter(entity=entity).order_by("-created_at")[:REVIEWS_SHOWN]
    return AdDetailOut(
        entity_id=entity.pk,
        level=entity.level,
        row=built[entity.pk],
        children=kids,
        ads=beneath,
        connection_id=entity.connection_id,
        provider=entity.connection.provider,
        external_id=entity.external_id,
        name=entity.name,
        status=entity.status,
        review_status=entity.review_status,
        data=entity.data,
        campaign=_node(campaign) if campaign else None,
        ad_group=_node(group) if group else None,
        sync=_sync_out(entity.connection),
        days=[DayOut(**r) for r in stats.series(ids, days)],
        totals={
            "last_7_days": stats.totals(ids, today - timedelta(days=7), today),
            "last_30_days": stats.totals(ids, today - timedelta(days=30), today),
            "range": stats.totals(ids, since, today),
        },
        changes=[
            StoredChangeOut(at=c.at, level=c.entity.level, name=c.entity.name, diff=c.diff)
            for c in changes
        ],
        reviews=[_review_summary(r) for r in recent],
        platform=provider.platform_view(entity),
        checks=provider.run_checks(entity, account),
    )


@router.post(
    "/store/entities/{entity_id}/reviews",
    response=ReviewOut,
    operation_id="openai_ads_store_review_entity",
)
def review_entity(request: HttpRequest, entity_id: uuid.UUID) -> ReviewOut:
    """Ask Claude to review this ad; the answer is stored and compared with the last review."""
    entity = _entity(request, entity_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    return _review_out(reviews.start(entity.connection, user.pk, entity=entity))


@router.post(
    "/store/account-review", response=ReviewOut, operation_id="openai_ads_store_review_account"
)
def review_account(request: HttpRequest, connection_id: uuid.UUID) -> ReviewOut:
    connection = _connection(request, connection_id, Permission.EDIT_CONTENT)
    user: User = request.auth  # type: ignore[attr-defined]
    return _review_out(reviews.start(connection, user.pk))


@router.get(
    "/store/reviews/{review_id}", response=ReviewOut, operation_id="openai_ads_store_review_get"
)
def get_review(request: HttpRequest, review_id: uuid.UUID) -> ReviewOut:
    membership = resolve_workspace(request, Permission.VIEW)
    return _review_out(reviews.get(membership.workspace_id, review_id))


@router.get(
    "/store/account-reviews",
    response=list[ReviewSummaryOut],
    operation_id="openai_ads_store_account_reviews",
)
def account_reviews(request: HttpRequest, connection_id: uuid.UUID) -> list[ReviewSummaryOut]:
    connection = _connection(request, connection_id, Permission.VIEW)
    rows = AdReview.objects.filter(connection=connection, scope=ReviewScope.ACCOUNT).order_by(
        "-created_at"
    )[:REVIEWS_SHOWN]
    return [_review_summary(r) for r in rows]


class AlertOut(Schema):
    id: uuid.UUID
    code: str
    severity: str
    params: dict[str, Any]
    entity_id: uuid.UUID | None
    entity_name: str
    first_seen_at: datetime
    last_seen_at: datetime
    acknowledged: bool


def _alert_out(alert: AdAlert) -> AlertOut:
    return AlertOut(
        id=alert.pk,
        code=alert.code,
        severity=alert.severity,
        params=alert.params,
        entity_id=alert.entity_id,
        entity_name=alert.entity.name if alert.entity else "",
        first_seen_at=alert.first_seen_at,
        last_seen_at=alert.last_seen_at,
        acknowledged=alert.acknowledged,
    )


@router.get("/store/alerts", response=list[AlertOut], operation_id="openai_ads_store_alerts")
def list_alerts(request: HttpRequest, connection_id: uuid.UUID) -> list[AlertOut]:
    """Open alerts for an ad account, most serious first."""
    connection = _connection(request, connection_id, Permission.VIEW)
    order = {"high": 0, "medium": 1, "low": 2}
    rows = AdAlert.objects.filter(connection=connection, resolved_at__isnull=True).select_related(
        "entity"
    )
    return [
        _alert_out(a)
        for a in sorted(rows, key=lambda a: (order.get(a.severity, 3), a.acknowledged))
    ]


@router.post(
    "/store/alerts/{alert_id}/acknowledge",
    response=AlertOut,
    operation_id="openai_ads_store_alert_acknowledge",
)
def acknowledge_alert(request: HttpRequest, alert_id: uuid.UUID) -> AlertOut:
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    alert = AdAlert.objects.filter(pk=alert_id, workspace_id=membership.workspace_id).first()
    if alert is None:
        raise NotFoundError("alert not found")
    alert.acknowledged = True
    alert.save(update_fields=["acknowledged", "updated_at"])
    return _alert_out(alert)
