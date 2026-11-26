"""Charging: turn a sent message into a usage record and a wallet debit, exactly once."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from audit.actors import Actor
from core.errors import NotFoundError
from crm.contacts import services as contacts
from crm.events.publisher import publish
from crm.inbox.models import Direction, Message
from crm.usage import events, rating, wallet
from crm.usage.models import UsageRecord
from crm.usage.units import category_of, units_for
from workspaces.models import Workspace

MARKUP_KEY = "markup_percent"
BILLING_MODE_KEY = "billing_mode"
SPEND_CAP_KEY = "daily_spend_cap_micros"
PREPAID = "prepaid"


def _context(workspace_id: uuid.UUID) -> tuple[Workspace, str]:
    workspace = Workspace.objects.select_related("organization").get(pk=workspace_id)
    return workspace, str(settings.CRM_WALLET_CURRENCY)


def _country(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> str:
    try:
        contact = contacts.get_contact(workspace_id, contact_id, include_deleted=True)
    except NotFoundError:  # an erased contact must not stop the charge
        return ""
    return rating.country_of(contact.phone, contact.country)


def quote(
    *,
    workspace_id: uuid.UUID,
    channel: str,
    body: str,
    country: str = "",
    category: str = "",
    at: datetime | None = None,
) -> tuple[int, int, int, uuid.UUID | None]:
    """(units, cost, price, rate id) for a message that has not been sent yet."""
    workspace, currency = _context(workspace_id)
    units = units_for(channel, body)
    rate = rating.find_rate(
        channel=channel,
        currency=currency,
        at=at or timezone.now(),
        country=country,
        category=category,
    )
    if rate is None:
        return units, 0, 0, None
    percent = int(workspace.organization.limits.get(MARKUP_KEY, 0))
    return (
        units,
        rate.cost_micros * units,
        rating.with_markup(rate.price_micros * units, percent),
        rate.pk,
    )


def charge_message(*, workspace_id: uuid.UUID, message_id: uuid.UUID) -> UsageRecord | None:
    """Record the usage of a sent message and debit the wallet. Repeating it changes nothing."""
    message = (
        Message.objects.for_workspace(workspace_id)
        .select_related("conversation")
        .filter(pk=message_id, direction=Direction.OUTBOUND)
        .first()
    )
    if message is None:
        return None
    existing = UsageRecord.objects.filter(message=message).first()
    if existing is not None:
        return existing
    workspace, currency = _context(workspace_id)
    country = _country(workspace_id, message.conversation.contact_id)
    units = units_for(message.channel, message.body)
    rate = rating.find_rate(
        channel=message.channel,
        currency=currency,
        at=message.created_at,
        country=country,
        category=category_of(message.channel, message.attachments),
    )
    percent = int(workspace.organization.limits.get(MARKUP_KEY, 0))
    cost = rate.cost_micros * units if rate else 0
    price = rating.with_markup(rate.price_micros * units, percent) if rate else 0
    try:
        with transaction.atomic():
            record = UsageRecord.objects.create(
                workspace_id=workspace_id,
                message=message,
                channel=message.channel,
                units=units,
                currency=currency,
                cost_micros=cost,
                price_micros=price,
                rate_card=rate,
                rated=rate is not None,
            )
            if price:
                wallet.debit(
                    workspace.organization,
                    amount_micros=price,
                    reference=f"usage:{message.pk}",
                    workspace_id=workspace_id,
                )
    except IntegrityError:  # charged by a parallel run
        return UsageRecord.objects.get(message=message)
    _alert_if_low(workspace, workspace_id)
    return record


def _alert_if_low(workspace: Workspace, workspace_id: uuid.UUID) -> None:
    current = wallet.get_wallet(workspace.organization)
    if current.balance_micros <= current.low_balance_micros and wallet.mark_low_alerted(
        workspace.organization
    ):
        with transaction.atomic():
            publish(
                "crm.wallet.low",
                workspace_id=workspace_id,
                actor=Actor.system("billing"),
                data=events.WalletLow(
                    balance_micros=current.balance_micros, currency=current.currency
                ),
            )


def spent_today(workspace_id: uuid.UUID) -> int:
    """Price charged to this workspace since midnight UTC."""
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = (
        UsageRecord.objects.for_workspace(workspace_id)
        .filter(created_at__gte=start)
        .aggregate(total=Sum("price_micros"))["total"]
    )
    return int(total or 0)
