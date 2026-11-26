"""The ads list and AI-suggestion actions, from the local copy."""

import uuid
from datetime import timedelta

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from ads.models import AdEntity, AdLevel
from ads.openai import changes, fixes, stats
from ads.openai import review as reviews
from ads.openai.api_store import SyncOut, _connection, _sync_out
from ads.openai.rows import build_rows
from core.errors import ConflictError, ValidationFailedError
from identity.authentication import api_auth
from identity.models import User
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["ads-openai-store"], auth=api_auth)


class AdRowOut(Schema):
    entity_id: uuid.UUID
    level: str
    ads_count: int
    children_count: int
    name: str
    title: str
    body: str
    status: str
    review_status: str
    campaign: str
    ad_group: str
    impressions_7d: int
    clicks_7d: int
    spend_7d: float
    ctr_7d: float
    clicks_30d: int
    spend_30d: float
    conversions_7d: float = 0.0
    conversions_30d: float = 0.0
    issue_count: int
    bid_too_low: bool
    verdict: str | None
    score: int | None


class AdsListOut(Schema):
    sync: SyncOut
    currency: str
    totals_7d: dict[str, float]
    totals_30d: dict[str, float]
    ads: list[AdRowOut]


@router.get("/store/ads", response=AdsListOut, operation_id="openai_ads_store_ads")
def list_ads(request: HttpRequest, connection_id: uuid.UUID) -> AdsListOut:
    """Every ad with recent numbers and the platform's and the AI's verdicts. No platform call."""
    connection = _connection(request, connection_id, Permission.VIEW)
    today = timezone.now().date()
    ads = list(
        AdEntity.objects.filter(
            connection=connection, level=AdLevel.AD, missing_since__isnull=True
        ).select_related("parent__parent")
    )
    ids = [a.pk for a in ads]
    everything = list(AdEntity.objects.filter(connection=connection, missing_since__isnull=True))
    built = build_rows(everything)
    sync_out = _sync_out(connection)
    return AdsListOut(
        sync=sync_out,
        currency=sync_out.currency,
        totals_7d=stats.totals(ids, today - timedelta(days=7), today),
        totals_30d=stats.totals(ids, today - timedelta(days=30), today),
        ads=[AdRowOut(**built[a.pk]) for a in ads],
    )


class ApplyIn(Schema):
    apply_now: bool = False


class ApplyOut(Schema):
    action_id: uuid.UUID
    status: str
    applied: bool


@router.post(
    "/store/reviews/{review_id}/issues/{index}/apply",
    response=ApplyOut,
    operation_id="openai_ads_store_apply_fix",
)
def apply_fix(request: HttpRequest, review_id: uuid.UUID, index: int, body: ApplyIn) -> ApplyOut:
    """Turn one AI suggestion into a proposed change (approved at once if the user may)."""
    membership = resolve_workspace(request, Permission.EDIT_CONTENT)
    review = reviews.get(membership.workspace_id, review_id)
    user: User = request.auth  # type: ignore[attr-defined]
    issues = (review.result or {}).get("issues") or []
    if review.entity is None or not 0 <= index < len(issues) or not issues[index].get("fix"):
        raise ValidationFailedError("this suggestion cannot be applied")
    if str(index) in review.applied:
        raise ConflictError("this suggestion was already applied")
    kind, payload, summary = fixes.build_change(review.entity, issues[index]["fix"])
    action, applied = changes.submit(
        review.connection, user, kind, payload, summary=summary, apply_now=body.apply_now
    )
    review.applied = {
        **review.applied,
        str(index): {"action_id": str(action.pk), "at": timezone.now().isoformat()},
    }
    review.save(update_fields=["applied", "updated_at"])
    return ApplyOut(action_id=action.pk, status=action.status, applied=applied)
