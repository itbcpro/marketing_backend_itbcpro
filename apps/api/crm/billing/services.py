"""Invoices: one per organization and month, made once however often the job runs."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.errors import NotFoundError
from crm.billing.models import Invoice, InvoiceStatus
from crm.plans import services as plans
from crm.usage import services as usage
from workspaces.models import Organization


def previous_month(today: date) -> tuple[date, date]:
    """First and last day of the month before ``today``."""
    last = today.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def _bounds(first: date, last: date) -> tuple[datetime, datetime]:
    zone = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first, time.min), zone)
    end = timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min), zone)
    return start, end


def _number(organization: Organization, first: date) -> str:
    return f"INV-{first:%Y%m}-{organization.pk.hex[:8].upper()}"


def generate(organization: Organization, first: date, last: date) -> Invoice | None:
    """The invoice of one period, or ``None`` when there is nothing to charge.

    Running it again returns the invoice that already exists.
    """
    existing = Invoice.objects.filter(organization=organization, period_start=first).first()
    if existing is not None:
        return existing
    plan = plans.plan_of(organization)
    since, until = _bounds(first, last)
    per_channel = usage.usage_by_channel(organization.pk, since, until)
    usage_total = sum(row["price_micros"] for row in per_channel)
    if plan.price_micros == 0 and usage_total == 0:
        return None
    lines: list[dict[str, Any]] = []
    if plan.price_micros:
        lines.append(
            {"kind": "plan", "channel": "", "units": 1, "amount_micros": plan.price_micros}
        )
    lines += [
        {
            "kind": "usage",
            "channel": r["channel"],
            "units": r["units"],
            "amount_micros": r["price_micros"],
        }
        for r in per_channel
        if r["price_micros"]
    ]
    prepaid = usage.billing_mode_of(organization) == usage.PREPAID
    due = plan.price_micros + (0 if prepaid else usage_total)
    try:
        with transaction.atomic():
            return Invoice.objects.create(
                organization=organization,
                number=_number(organization, first),
                period_start=first,
                period_end=last,
                currency=str(settings.CRM_WALLET_CURRENCY),
                plan_key=plan.key,
                plan_fee_micros=plan.price_micros,
                usage_micros=usage_total,
                amount_due_micros=due,
                lines=lines,
                status=InvoiceStatus.ISSUED if due else InvoiceStatus.PAID,
                paid_at=None if due else timezone.now(),
            )
    except IntegrityError:  # a parallel run made it first
        return Invoice.objects.get(organization=organization, period_start=first)


def generate_all(today: date | None = None) -> int:
    """Invoice every active organization for the month that just ended. Returns how many are new."""
    first, last = previous_month(today or timezone.localdate())
    made = 0
    for organization in Organization.objects.filter(is_active=True):
        had = Invoice.objects.filter(organization=organization, period_start=first).exists()
        if not had and generate(organization, first, last) is not None:
            made += 1
    return made


def list_invoices(organization_id: uuid.UUID) -> list[Invoice]:
    return list(Invoice.objects.filter(organization_id=organization_id))


def get_invoice(organization_id: uuid.UUID, invoice_id: uuid.UUID) -> Invoice:
    found = Invoice.objects.filter(organization_id=organization_id, pk=invoice_id).first()
    if found is None:
        raise NotFoundError("invoice not found")
    return found


def mark_paid(invoice: Invoice) -> Invoice:
    if invoice.status == InvoiceStatus.ISSUED:
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = timezone.now()
        invoice.save(update_fields=["status", "paid_at", "updated_at"])
    return invoice
