"""Best-practice review of a campaign draft before it is sent for approval."""

import uuid
from typing import Any, Literal

from django.http import HttpRequest
from ninja import Router, Schema
from pydantic import ValidationError

from ads.google import drafter, practice
from ads.google.api import _connection
from ads.google.schemas import PmaxCampaign, SearchCampaign
from ads.models import AdEntity, AdLevel, AdSyncState
from core.errors import NotFoundError
from identity.authentication import api_auth
from workspaces.permissions import Permission

router = Router(tags=["ads-google"], auth=api_auth)


class PracticeIn(Schema):
    connection_id: uuid.UUID
    kind: Literal["search", "pmax"]
    draft: dict[str, Any]


class PracticeCheck(Schema):
    code: str
    level: Literal["fail", "warn", "info", "ok"]
    params: dict[str, Any]


class PracticeOut(Schema):
    complete: bool
    score: int
    checks: list[PracticeCheck]


def _tracking(connection_id: uuid.UUID) -> bool | None:
    """True when an enabled primary conversion action exists; None before the first sync."""
    state = AdSyncState.objects.filter(connection=connection_id).first()
    if state is None:
        return None
    actions = (state.account or {}).get("conversion_actions")
    if actions is None:
        return None
    return any(a.get("primary") and a.get("status") == "enabled" for a in actions)


@router.post("/practice", response=PracticeOut, operation_id="google_ads_practice")
def review_draft(request: HttpRequest, body: PracticeIn) -> PracticeOut:
    """Judge a draft by best practice; an unfinished form is reported as incomplete."""
    connection = _connection(request, body.connection_id, Permission.VIEW)
    payload = {**body.draft, "connection_id": connection.pk}
    try:
        if body.kind == "search":
            checks = practice.review_search(
                SearchCampaign(**payload), tracking=_tracking(connection.pk)
            )
        else:
            checks = practice.review_pmax(
                PmaxCampaign(**payload), tracking=_tracking(connection.pk)
            )
    except ValidationError:
        return PracticeOut(complete=False, score=0, checks=[])
    score = practice.summarize(checks)["score"]
    return PracticeOut(complete=True, score=score, checks=checks)  # type: ignore[arg-type]


class DraftIn(Schema):
    connection_id: uuid.UUID
    brief: str
    website: str = ""
    currency: str = ""


class DraftKeywordOut(Schema):
    text: str
    match_type: Literal["exact", "phrase", "broad"]


class DraftAdOut(Schema):
    final_url: str
    headlines: list[str]
    descriptions: list[str]
    path1: str
    path2: str


class DraftGroupOut(Schema):
    name: str
    keywords: list[DraftKeywordOut]
    negative_keywords: list[DraftKeywordOut]
    ads: list[DraftAdOut]


class DraftCampaignOut(Schema):
    name: str
    daily_budget_micros: int
    bidding: Literal["manual_cpc", "maximize_clicks", "maximize_conversions"]
    max_cpc_micros: int | None
    countries: list[str]
    languages: list[str]
    negative_keywords: list[DraftKeywordOut]
    ad_groups: list[DraftGroupOut]


class DraftOut(Schema):
    summary: str
    notes: str
    campaigns: list[DraftCampaignOut]


@router.post("/draft", response=DraftOut, operation_id="google_ads_draft")
def draft_campaign(request: HttpRequest, body: DraftIn) -> DraftOut:
    """Write a complete Search campaign draft from a plain description (nothing is created)."""
    connection = _connection(request, body.connection_id, Permission.EDIT_CONTENT)
    return DraftOut(**drafter.draft(connection, body.brief, body.website, body.currency))


class GroupDraftIn(Schema):
    connection_id: uuid.UUID
    campaign_id: str
    brief: str
    website: str = ""
    currency: str = ""


class GroupDraftOut(Schema):
    summary: str
    notes: str
    ad_group: DraftGroupOut


@router.post("/draft-ad-group", response=GroupDraftOut, operation_id="google_ads_draft_ad_group")
def draft_ad_group(request: HttpRequest, body: GroupDraftIn) -> GroupDraftOut:
    """Write one more ad group for an existing campaign (nothing is created)."""
    connection = _connection(request, body.connection_id, Permission.EDIT_CONTENT)
    campaign = AdEntity.objects.filter(
        connection=connection, level=AdLevel.CAMPAIGN, external_id=body.campaign_id
    ).first()
    if campaign is None:
        raise NotFoundError("sync the account first: this campaign is not in the local copy")
    return GroupDraftOut(
        **drafter.draft_group(connection, campaign, body.brief, body.website, body.currency)
    )
