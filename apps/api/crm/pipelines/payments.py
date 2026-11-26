"""Payments recorded against a deal: a ledger kept by hand, with no card or provider involved.

Taking money online (checkout, invoices, payment links) needs a payment provider and is a separate
task; this module only records what was received or given back, so a deal shows what is paid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import QuerySet, Sum
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import NotFoundError, ValidationFailedError
from crm.events.publisher import publish
from crm.pipelines import clean, events
from crm.pipelines.models import (
    Opportunity,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)

REFERENCE_LIMIT = 100
NOTE_LIMIT = 1_000
MAX_MICROS = 10**15


@dataclass(frozen=True)
class Summary:
    currency: str
    deal_micros: int
    paid_micros: int
    refunded_micros: int
    pending_micros: int

    @property
    def net_micros(self) -> int:
        return self.paid_micros - self.refunded_micros

    @property
    def balance_micros(self) -> int:
        return max(self.deal_micros - self.net_micros, 0)


def listing(workspace_id: uuid.UUID, deal: Opportunity) -> QuerySet[Payment]:
    rows = Payment.objects.for_workspace(workspace_id).filter(opportunity=deal)
    return rows.order_by("-occurred_at", "-created_at")


def _total(rows: QuerySet[Payment], kind: str, status: str) -> int:
    return int(rows.filter(kind=kind, status=status).aggregate(t=Sum("amount_micros"))["t"] or 0)


def summary(workspace_id: uuid.UUID, deal: Opportunity) -> Summary:
    rows = listing(workspace_id, deal)
    return Summary(
        currency=deal.currency,
        deal_micros=deal.value_micros,
        paid_micros=_total(rows, PaymentKind.PAYMENT, PaymentStatus.PAID),
        refunded_micros=_total(rows, PaymentKind.REFUND, PaymentStatus.PAID),
        pending_micros=_total(rows, PaymentKind.PAYMENT, PaymentStatus.PENDING),
    )


def get_payment(workspace_id: uuid.UUID, payment_id: uuid.UUID) -> Payment:
    found = (
        Payment.objects.for_workspace(workspace_id)
        .select_related("opportunity")
        .filter(pk=payment_id)
        .first()
    )
    if found is None:
        raise NotFoundError("payment not found")
    return found


def _choice(value: str, allowed: list[str], field: str) -> str:
    if value not in allowed:
        raise ValidationFailedError("invalid value", errors={field: "invalid_value"})
    return value


def _text(value: str, field: str, limit: int) -> str:
    clean_value = value.strip()
    if len(clean_value) > limit:
        raise ValidationFailedError("too long", errors={field: "too_long"})
    return clean_value


def _amount(micros: int) -> int:
    if micros <= 0 or micros > MAX_MICROS:
        raise ValidationFailedError("invalid amount", errors={"amount_micros": "invalid_value"})
    return micros


def _check_refund(workspace_id: uuid.UUID, deal: Opportunity, amount: int, *, exclude: Any) -> None:
    """A refund may not give back more than has been received."""
    rows = listing(workspace_id, deal)
    if exclude is not None:
        rows = rows.exclude(pk=exclude)
    net = _total(rows, PaymentKind.PAYMENT, PaymentStatus.PAID) - _total(
        rows, PaymentKind.REFUND, PaymentStatus.PAID
    )
    if amount > net:
        raise ValidationFailedError(
            "refund exceeds payments", errors={"amount_micros": "exceeds_paid"}
        )


def record(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    deal: Opportunity,
    kind: str,
    amount_micros: int,
    status: str = PaymentStatus.PAID,
    method: str = PaymentMethod.OTHER,
    reference: str = "",
    note: str = "",
    occurred_at: datetime | None = None,
    currency: str | None = None,
) -> Payment:
    kind = _choice(kind, PaymentKind.values, "kind")
    status = _choice(status, PaymentStatus.values, "status")
    method = _choice(method, PaymentMethod.values, "method")
    amount = _amount(amount_micros)
    code = clean.currency(currency) if currency else deal.currency
    if code != deal.currency:
        raise ValidationFailedError("currency differs", errors={"currency": "not_deal_currency"})
    when = occurred_at or timezone.now()
    if timezone.is_naive(when):
        raise ValidationFailedError("time zone required", errors={"occurred_at": "invalid_value"})
    with transaction.atomic():
        if kind == PaymentKind.REFUND and status == PaymentStatus.PAID:
            _check_refund(workspace_id, deal, amount, exclude=None)
        payment = Payment.objects.create(
            workspace_id=workspace_id,
            contact_id=deal.contact_id,
            opportunity=deal,
            kind=kind,
            status=status,
            method=method,
            amount_micros=amount,
            currency=code,
            reference=_text(reference, "reference", REFERENCE_LIMIT),
            note=_text(note, "note", NOTE_LIMIT),
            occurred_at=when,
            recorded_by_id=uuid.UUID(actor.id) if actor.type == "human" else None,
        )
        publish(
            "crm.payment.recorded",
            workspace_id=workspace_id,
            actor=actor,
            data=events.PaymentRecorded(
                payment_id=payment.pk,
                opportunity_id=deal.pk,
                contact_id=deal.contact_id,
                kind=kind,
                status=status,
                amount_micros=amount,
                currency=code,
            ),
        )
    return payment


def update(
    *, workspace_id: uuid.UUID, actor: Actor, payment_id: uuid.UUID, data: dict[str, Any]
) -> Payment:
    with transaction.atomic():
        payment = get_payment(workspace_id, payment_id)
        deal = payment.opportunity
        if "status" in data:
            payment.status = _choice(data["status"], PaymentStatus.values, "status")
        if "method" in data:
            payment.method = _choice(data["method"], PaymentMethod.values, "method")
        if "amount_micros" in data:
            payment.amount_micros = _amount(data["amount_micros"])
        if "reference" in data:
            payment.reference = _text(data["reference"] or "", "reference", REFERENCE_LIMIT)
        if "note" in data:
            payment.note = _text(data["note"] or "", "note", NOTE_LIMIT)
        if data.get("occurred_at") is not None:
            if timezone.is_naive(data["occurred_at"]):
                raise ValidationFailedError(
                    "time zone required", errors={"occurred_at": "invalid_value"}
                )
            payment.occurred_at = data["occurred_at"]
        if payment.kind == PaymentKind.REFUND and payment.status == PaymentStatus.PAID:
            _check_refund(workspace_id, deal, payment.amount_micros, exclude=payment.pk)
        elif payment.kind == PaymentKind.PAYMENT and payment.status != PaymentStatus.PAID:
            _check_refunds_still_covered(workspace_id, deal, payment)
        payment.save()
        record_event(
            action="crm.payment.updated",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_payment",
            target_id=payment.pk,
        )
    return payment


def _check_refunds_still_covered(
    workspace_id: uuid.UUID, deal: Opportunity, payment: Payment
) -> None:
    """Un-paying a payment must not leave refunds larger than what remains received."""
    rows = listing(workspace_id, deal).exclude(pk=payment.pk)
    received = _total(rows, PaymentKind.PAYMENT, PaymentStatus.PAID)
    refunded = _total(rows, PaymentKind.REFUND, PaymentStatus.PAID)
    if refunded > received:
        raise ValidationFailedError("refunds exceed payments", errors={"status": "refunded"})


def delete(*, workspace_id: uuid.UUID, actor: Actor, payment_id: uuid.UUID) -> None:
    with transaction.atomic():
        payment = get_payment(workspace_id, payment_id)
        if payment.kind == PaymentKind.PAYMENT and payment.status == PaymentStatus.PAID:
            _check_refunds_still_covered(workspace_id, payment.opportunity, payment)
        payment.delete()
        record_event(
            action="crm.payment.deleted",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_payment",
            target_id=payment_id,
        )
