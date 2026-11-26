"""Public surface of the usage module: what the send gateway needs to know about money."""

from __future__ import annotations

import uuid

from crm.usage import charging, stats, wallet
from crm.usage.charging import charge_message, quote, spent_today
from crm.usage.models import Wallet
from workspaces.models import Organization, Workspace


def billing_mode(workspace_id: uuid.UUID) -> str:
    organization = (
        Workspace.objects.select_related("organization").get(pk=workspace_id).organization
    )
    return str(organization.limits.get(charging.BILLING_MODE_KEY, "postpaid"))


def daily_spend_cap(workspace_id: uuid.UUID) -> int | None:
    organization = (
        Workspace.objects.select_related("organization").get(pk=workspace_id).organization
    )
    cap = organization.limits.get(charging.SPEND_CAP_KEY)
    return None if cap is None else int(cap)


def can_afford(*, workspace_id: uuid.UUID, price_micros: int) -> bool:
    """Prepaid organizations need the balance to cover the message; others always can."""
    if billing_mode(workspace_id) != charging.PREPAID:
        return True
    organization = (
        Workspace.objects.select_related("organization").get(pk=workspace_id).organization
    )
    return wallet.get_wallet(organization).balance_micros >= price_micros


def wallet_of(workspace_id: uuid.UUID) -> Wallet:
    organization = (
        Workspace.objects.select_related("organization").get(pk=workspace_id).organization
    )
    return wallet.get_wallet(organization)


__all__ = [
    "billing_mode",
    "can_afford",
    "charge_message",
    "daily_spend_cap",
    "quote",
    "spent_today",
    "wallet_of",
]

usage_by_channel = stats.usage_by_channel
PREPAID = charging.PREPAID


def billing_mode_of(organization: Organization) -> str:
    """How an organization pays for usage: from its wallet (prepaid) or on the invoice."""
    return str(organization.limits.get(charging.BILLING_MODE_KEY, "postpaid"))
