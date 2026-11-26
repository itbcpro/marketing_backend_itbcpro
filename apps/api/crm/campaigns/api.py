"""Campaign endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.api.pagination import DEFAULT_LIMIT, Page, paginate
from crm.campaigns import services
from crm.campaigns.models import Campaign, CampaignRecipient
from crm.consent.models import Purpose
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.models import Membership
from workspaces.permissions import Permission

router = Router(tags=["crm-campaigns"], auth=api_auth)


class CampaignIn(Schema):
    name: str
    channel: str
    body: str = ""
    subject: str = ""
    purpose: str = Purpose.MARKETING
    audience: dict[str, Any] = {}
    channel_account_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    batch_size: int | None = None
    batch_interval_seconds: int | None = None
    track_opens: bool = False
    track_clicks: bool = False
    variant_subject: str = ""
    variant_body: str = ""
    variant_percent: int = 0


class CampaignPatch(Schema):
    name: str | None = None
    channel: str | None = None
    body: str | None = None
    subject: str | None = None
    purpose: str | None = None
    audience: dict[str, Any] | None = None
    batch_size: int | None = None
    batch_interval_seconds: int | None = None
    track_opens: bool | None = None
    track_clicks: bool | None = None
    variant_subject: str | None = None
    variant_body: str | None = None
    variant_percent: int | None = None


class LaunchIn(Schema):
    scheduled_at: datetime | None = None


class CampaignOut(Schema):
    id: uuid.UUID
    name: str
    channel: str
    purpose: str
    subject: str
    body: str
    audience: dict[str, Any]
    track_opens: bool
    track_clicks: bool
    variant_subject: str
    variant_body: str
    variant_percent: int
    status: str
    scheduled_at: datetime | None
    batch_size: int
    batch_interval_seconds: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RecipientOut(Schema):
    contact_id: uuid.UUID
    status: str
    reason: str
    processed_at: datetime | None


def _actor(m: Membership) -> Actor:
    return Actor.human(m.user_id)


def _out(row: Campaign) -> CampaignOut:
    return CampaignOut.model_validate(row, from_attributes=True)


@router.get("/campaigns", response=Page[CampaignOut], operation_id="crm_campaigns_list")
def list_campaigns(
    request: HttpRequest, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> Page[CampaignOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = Campaign.objects.for_workspace(membership.workspace_id)
    page, next_cursor = paginate(rows, cursor=cursor, limit=limit)
    return Page[CampaignOut](items=[_out(r) for r in page], next_cursor=next_cursor)


@router.post("/campaigns", response={201: CampaignOut}, operation_id="crm_campaigns_create")
def create_campaign(request: HttpRequest, payload: CampaignIn) -> Status[CampaignOut]:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    row = services.create(membership=membership, actor=_actor(membership), **payload.model_dump())
    return Status(201, _out(row))


@router.get("/campaigns/{campaign_id}", response=CampaignOut, operation_id="crm_campaigns_get")
def get_campaign(request: HttpRequest, campaign_id: uuid.UUID) -> CampaignOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    return _out(services.get(membership.workspace_id, campaign_id))


@router.patch("/campaigns/{campaign_id}", response=CampaignOut, operation_id="crm_campaigns_update")
def update_campaign(
    request: HttpRequest, campaign_id: uuid.UUID, payload: CampaignPatch
) -> CampaignOut:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    row = services.update(
        membership=membership,
        actor=_actor(membership),
        campaign_id=campaign_id,
        data=payload.model_dump(exclude_unset=True, exclude_none=True),
    )
    return _out(row)


@router.post(
    "/campaigns/{campaign_id}/launch", response=CampaignOut, operation_id="crm_campaigns_launch"
)
def launch_campaign(request: HttpRequest, campaign_id: uuid.UUID, payload: LaunchIn) -> CampaignOut:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    row = services.launch(
        workspace_id=membership.workspace_id,
        actor=_actor(membership),
        campaign_id=campaign_id,
        scheduled_at=payload.scheduled_at,
    )
    return _out(row)


@router.post(
    "/campaigns/{campaign_id}/pause", response=CampaignOut, operation_id="crm_campaigns_pause"
)
def pause_campaign(request: HttpRequest, campaign_id: uuid.UUID) -> CampaignOut:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    return _out(services.pause(workspace_id=membership.workspace_id, campaign_id=campaign_id))


@router.post(
    "/campaigns/{campaign_id}/resume", response=CampaignOut, operation_id="crm_campaigns_resume"
)
def resume_campaign(request: HttpRequest, campaign_id: uuid.UUID) -> CampaignOut:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    return _out(services.resume(workspace_id=membership.workspace_id, campaign_id=campaign_id))


@router.post(
    "/campaigns/{campaign_id}/cancel", response=CampaignOut, operation_id="crm_campaigns_cancel"
)
def cancel_campaign(request: HttpRequest, campaign_id: uuid.UUID) -> CampaignOut:
    membership = resolve_workspace(request, Permission.CRM_SEND)
    row = services.cancel(
        workspace_id=membership.workspace_id, actor=_actor(membership), campaign_id=campaign_id
    )
    return _out(row)


@router.get(
    "/campaigns/{campaign_id}/stats", response=dict[str, Any], operation_id="crm_campaigns_stats"
)
def campaign_stats(request: HttpRequest, campaign_id: uuid.UUID) -> dict[str, Any]:
    membership = resolve_workspace(request, Permission.CRM_VIEW_REPORTS)
    return services.stats(membership.workspace_id, campaign_id)


@router.get(
    "/campaigns/{campaign_id}/recipients",
    response=Page[RecipientOut],
    operation_id="crm_campaigns_recipients",
)
def campaign_recipients(
    request: HttpRequest,
    campaign_id: uuid.UUID,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[RecipientOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW_REPORTS)
    campaign = services.get(membership.workspace_id, campaign_id)
    rows = CampaignRecipient.objects.filter(campaign=campaign)
    if status:
        rows = rows.filter(status=status)
    page, next_cursor = paginate(rows, cursor=cursor, limit=limit)
    items = [RecipientOut.model_validate(r, from_attributes=True) for r in page]
    return Page[RecipientOut](items=items, next_cursor=next_cursor)
