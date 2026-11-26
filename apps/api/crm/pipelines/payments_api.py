"""Payment endpoints of a deal."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.errors import NotFoundError
from crm.pipelines import payments, services
from crm.pipelines.models import Payment
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-payments"], auth=api_auth)


class PaymentIn(Schema):
    kind: str = "payment"
    amount_micros: int
    status: str = "paid"
    method: str = "other"
    reference: str = ""
    note: str = ""
    occurred_at: datetime | None = None
    currency: str | None = None


class PaymentPatch(Schema):
    amount_micros: int | None = None
    status: str | None = None
    method: str | None = None
    reference: str | None = None
    note: str | None = None
    occurred_at: datetime | None = None


class PaymentOut(Schema):
    id: uuid.UUID
    opportunity_id: uuid.UUID
    kind: str
    status: str
    method: str
    amount_micros: int
    currency: str
    reference: str
    note: str
    occurred_at: datetime
    created_at: datetime


class PaymentSummaryOut(Schema):
    currency: str
    deal_micros: int
    paid_micros: int
    refunded_micros: int
    pending_micros: int
    net_micros: int
    balance_micros: int


class PaymentsOut(Schema):
    items: list[PaymentOut]
    summary: PaymentSummaryOut


def _out(row: Payment) -> PaymentOut:
    return PaymentOut(
        id=row.pk,
        opportunity_id=row.opportunity_id,
        kind=row.kind,
        status=row.status,
        method=row.method,
        amount_micros=row.amount_micros,
        currency=row.currency,
        reference=row.reference,
        note=row.note,
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


@router.get(
    "/opportunities/{deal_id}/payments", response=PaymentsOut, operation_id="crm_payments_list"
)
def list_payments(request: HttpRequest, deal_id: uuid.UUID) -> PaymentsOut:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    deal = services.get_opportunity(membership.workspace_id, deal_id, membership)
    total = payments.summary(membership.workspace_id, deal)
    return PaymentsOut(
        items=[_out(r) for r in payments.listing(membership.workspace_id, deal)],
        summary=PaymentSummaryOut(
            currency=total.currency,
            deal_micros=total.deal_micros,
            paid_micros=total.paid_micros,
            refunded_micros=total.refunded_micros,
            pending_micros=total.pending_micros,
            net_micros=total.net_micros,
            balance_micros=total.balance_micros,
        ),
    )


@router.post(
    "/opportunities/{deal_id}/payments",
    response={201: PaymentOut},
    operation_id="crm_payments_record",
)
def record_payment(
    request: HttpRequest, deal_id: uuid.UUID, payload: PaymentIn
) -> Status[PaymentOut]:
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    deal = services.get_opportunity(membership.workspace_id, deal_id, membership)
    row = payments.record(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        deal=deal,
        **payload.model_dump(),
    )
    return Status(201, _out(row))


def _visible(request: HttpRequest, payment_id: uuid.UUID, permission: Permission) -> Payment:
    membership = resolve_workspace(request, permission)
    row = payments.get_payment(membership.workspace_id, payment_id)
    try:
        services.get_opportunity(membership.workspace_id, row.opportunity_id, membership)
    except NotFoundError:
        raise NotFoundError("payment not found") from None
    return row


@router.patch("/payments/{payment_id}", response=PaymentOut, operation_id="crm_payments_update")
def update_payment(
    request: HttpRequest, payment_id: uuid.UUID, payload: PaymentPatch
) -> PaymentOut:
    row = _visible(request, payment_id, Permission.CRM_EDIT)
    membership = resolve_workspace(request, Permission.CRM_EDIT)
    updated = payments.update(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        payment_id=row.pk,
        data=payload.model_dump(exclude_unset=True),
    )
    return _out(updated)


@router.delete("/payments/{payment_id}", response={204: None}, operation_id="crm_payments_delete")
def delete_payment(request: HttpRequest, payment_id: uuid.UUID) -> Status[None]:
    row = _visible(request, payment_id, Permission.CRM_DELETE)
    membership = resolve_workspace(request, Permission.CRM_DELETE)
    payments.delete(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        payment_id=row.pk,
    )
    return Status(204, None)
