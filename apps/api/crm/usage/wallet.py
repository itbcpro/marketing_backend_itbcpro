"""The prepaid wallet. Each movement is a ledger row with a unique reference, so it runs once."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import IntegrityError, transaction

from core.errors import ValidationFailedError
from crm.usage.models import TransactionKind, Wallet, WalletTransaction
from workspaces.models import Organization


def get_wallet(organization: Organization) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(
        organization=organization, defaults={"currency": settings.CRM_WALLET_CURRENCY}
    )
    return wallet


def _move(
    organization: Organization,
    *,
    kind: str,
    amount_micros: int,
    reference: str,
    note: str = "",
    workspace_id: uuid.UUID | None = None,
) -> tuple[WalletTransaction, bool]:
    """Apply one signed amount. Returns the entry and whether it was newly applied."""
    get_wallet(organization)
    try:
        with transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(organization=organization)
            existing = WalletTransaction.objects.filter(wallet=wallet, reference=reference).first()
            if existing is not None:
                return existing, False
            wallet.balance_micros += amount_micros
            if wallet.balance_micros > wallet.low_balance_micros:
                wallet.low_alerted = False
            wallet.save(update_fields=["balance_micros", "low_alerted", "updated_at"])
            entry = WalletTransaction.objects.create(
                wallet=wallet,
                kind=kind,
                amount_micros=amount_micros,
                balance_after_micros=wallet.balance_micros,
                reference=reference,
                note=note[:300],
                workspace_id=workspace_id,
            )
            return entry, True
    except IntegrityError:  # the same reference won a race
        wallet = Wallet.objects.get(organization=organization)
        return WalletTransaction.objects.get(wallet=wallet, reference=reference), False


def top_up(
    organization: Organization, *, amount_micros: int, reference: str, note: str = ""
) -> WalletTransaction:
    if amount_micros <= 0 or not reference:
        raise ValidationFailedError("invalid top-up", errors={"amount": "must_be_positive"})
    entry, _ = _move(
        organization,
        kind=TransactionKind.TOP_UP,
        amount_micros=amount_micros,
        reference=reference,
        note=note,
    )
    return entry


def debit(
    organization: Organization, *, amount_micros: int, reference: str, workspace_id: uuid.UUID
) -> tuple[WalletTransaction, bool]:
    """Charge usage. The balance may go below zero: usage that happened is always recorded."""
    return _move(
        organization,
        kind=TransactionKind.USAGE,
        amount_micros=-amount_micros,
        reference=reference,
        workspace_id=workspace_id,
    )


def mark_low_alerted(organization: Organization) -> bool:
    """Flag the low-balance alert as sent. True only for the call that raised it."""
    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(organization=organization)
        if wallet.low_alerted or wallet.balance_micros > wallet.low_balance_micros:
            return False
        wallet.low_alerted = True
        wallet.save(update_fields=["low_alerted", "updated_at"])
        return True
