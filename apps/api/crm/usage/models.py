"""Usage metering: rate cards, a prepaid wallet per organization, and one record per charge.

Money is integer micros (millionths of a currency unit), never floats.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel, TenantManager, TenantModel


class RateCard(BaseModel):
    """What a unit costs us and what it is priced at, from a date on. Platform-wide data.

    Rows are never edited: a new price is a new row with a later ``effective_from``, so past usage
    keeps the rate it was charged at. The most specific matching row wins.
    """

    channel = models.CharField(max_length=20)
    country = models.CharField(max_length=2, blank=True, default="")  # empty means any country
    category = models.CharField(max_length=30, blank=True, default="")  # empty means any category
    currency = models.CharField(max_length=3)
    cost_micros = models.BigIntegerField()
    price_micros = models.BigIntegerField()
    effective_from = models.DateTimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(cost_micros__gte=0) & Q(price_micros__gte=0), name="rate_non_negative"
            ),
            models.UniqueConstraint(
                fields=["channel", "country", "category", "currency", "effective_from"],
                name="uniq_rate_card",
            ),
        ]
        indexes = [models.Index(fields=["channel", "-effective_from"], name="rate_lookup_idx")]


class Wallet(BaseModel):
    """The prepaid balance of one organization. Changed only through ``crm.usage.wallet``."""

    organization = models.OneToOneField(
        "workspaces.Organization", on_delete=models.CASCADE, related_name="wallet"
    )
    currency = models.CharField(max_length=3)
    balance_micros = models.BigIntegerField(default=0)
    low_balance_micros = models.BigIntegerField(default=0)
    low_alerted = models.BooleanField(default=False)


class TransactionKind(models.TextChoices):
    TOP_UP = "top_up"
    USAGE = "usage"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


class WalletTransaction(BaseModel):
    """An entry of the wallet ledger. ``reference`` makes each money movement happen once."""

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="transactions")
    kind = models.CharField(max_length=12, choices=TransactionKind.choices)
    amount_micros = models.BigIntegerField()
    balance_after_micros = models.BigIntegerField()
    reference = models.CharField(max_length=120)
    note = models.CharField(max_length=300, blank=True, default="")
    workspace_id = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["wallet", "reference"], name="uniq_wallet_reference")
        ]
        ordering = ["-created_at", "-id"]


class UsageRecord(TenantModel):
    """What one message consumed and cost. One per message, so retries never charge twice."""

    message = models.OneToOneField(
        "crm_inbox.Message", on_delete=models.CASCADE, related_name="usage"
    )
    channel = models.CharField(max_length=20)
    units = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    cost_micros = models.BigIntegerField(default=0)
    price_micros = models.BigIntegerField(default=0)
    rate_card = models.ForeignKey(RateCard, null=True, on_delete=models.SET_NULL, related_name="+")
    rated = models.BooleanField(default=True)

    objects = TenantManager["UsageRecord"]()

    class Meta:
        indexes = [models.Index(fields=["workspace", "-created_at"], name="usage_period_idx")]
