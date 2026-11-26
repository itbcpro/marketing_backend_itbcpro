"""What the workspace has used and what is left in its wallet."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db.models import Count, Sum
from django.http import HttpRequest
from ninja import Router, Schema

from core.api.pagination import DEFAULT_LIMIT, Page, paginate
from crm.usage import charging, services
from crm.usage.models import UsageRecord
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-usage"], auth=api_auth)


class WalletOut(Schema):
    currency: str
    balance_micros: int
    low_balance_micros: int
    billing_mode: str
    daily_spend_cap_micros: int | None
    spent_today_micros: int


class ChannelUsage(Schema):
    channel: str
    messages: int
    units: int
    price_micros: int


class UsageRecordOut(Schema):
    id: uuid.UUID
    message_id: uuid.UUID
    channel: str
    units: int
    currency: str
    price_micros: int
    rated: bool
    created_at: datetime


@router.get("/usage/wallet", response=WalletOut, operation_id="crm_usage_wallet")
def get_wallet(request: HttpRequest) -> WalletOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW_REPORTS)
    workspace_id = membership.workspace_id
    wallet = services.wallet_of(workspace_id)
    return WalletOut(
        currency=wallet.currency,
        balance_micros=wallet.balance_micros,
        low_balance_micros=wallet.low_balance_micros,
        billing_mode=services.billing_mode(workspace_id),
        daily_spend_cap_micros=services.daily_spend_cap(workspace_id),
        spent_today_micros=charging.spent_today(workspace_id),
    )


@router.get("/usage/summary", response=list[ChannelUsage], operation_id="crm_usage_summary")
def summary(
    request: HttpRequest, since: datetime | None = None, until: datetime | None = None
) -> list[ChannelUsage]:
    """Usage by channel for a period. Only the price the customer pays is shown, never our cost."""
    membership = resolve_workspace(request, Permission.CRM_VIEW_REPORTS)
    rows = UsageRecord.objects.for_workspace(membership.workspace_id)
    if since:
        rows = rows.filter(created_at__gte=since)
    if until:
        rows = rows.filter(created_at__lt=until)
    grouped = (
        rows.values("channel")
        .annotate(messages=Count("pk"), units=Sum("units"), price=Sum("price_micros"))
        .order_by("channel")
    )
    return [
        ChannelUsage(
            channel=g["channel"],
            messages=g["messages"],
            units=g["units"] or 0,
            price_micros=g["price"] or 0,
        )
        for g in grouped
    ]


@router.get("/usage/records", response=Page[UsageRecordOut], operation_id="crm_usage_records")
def records(
    request: HttpRequest, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> Page[UsageRecordOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW_REPORTS)
    rows = UsageRecord.objects.for_workspace(membership.workspace_id)
    page, next_cursor = paginate(rows, cursor=cursor, limit=limit)
    return Page[UsageRecordOut](
        items=[
            UsageRecordOut(
                id=r.pk,
                message_id=r.message_id,
                channel=r.channel,
                units=r.units,
                currency=r.currency,
                price_micros=r.price_micros,
                rated=r.rated,
                created_at=r.created_at,
            )
            for r in page
        ],
        next_cursor=next_cursor,
    )
