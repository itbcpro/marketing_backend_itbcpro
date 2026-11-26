"""Invoices of an organization: the plan fee plus the usage of the month."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel


class InvoiceStatus(models.TextChoices):
    ISSUED = "issued"
    PAID = "paid"
    VOID = "void"


class Invoice(BaseModel):
    organization = models.ForeignKey(
        "workspaces.Organization", on_delete=models.PROTECT, related_name="invoices"
    )
    number = models.CharField(max_length=40, unique=True)
    period_start = models.DateField()
    period_end = models.DateField()  # the last day of the period, inclusive
    currency = models.CharField(max_length=3)
    plan_key = models.CharField(max_length=50)
    plan_fee_micros = models.BigIntegerField(default=0)
    usage_micros = models.BigIntegerField(default=0)
    # What is still owed: usage on a prepaid wallet was paid when the wallet was topped up.
    amount_due_micros = models.BigIntegerField(default=0)
    # [{"kind": "plan"|"usage", "channel": str, "units": int, "amount_micros": int}]
    lines = models.JSONField(default=list)
    status = models.CharField(
        max_length=10, choices=InvoiceStatus.choices, default=InvoiceStatus.ISSUED
    )
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "period_start"], name="uniq_invoice_period"
            ),
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in InvoiceStatus]),
                name="invoice_status_valid",
            ),
            models.CheckConstraint(condition=Q(amount_due_micros__gte=0), name="invoice_due_ok"),
        ]
        ordering = ["-period_start"]
