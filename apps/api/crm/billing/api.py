"""Invoices of the organization behind the workspace. Only people who manage billing see them."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from crm.billing import services
from crm.billing.models import Invoice
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.models import Workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-billing"], auth=api_auth)


class InvoiceLine(Schema):
    kind: str
    channel: str
    units: int
    amount_micros: int


class InvoiceOut(Schema):
    id: uuid.UUID
    number: str
    period_start: date
    period_end: date
    currency: str
    plan_key: str
    plan_fee_micros: int
    usage_micros: int
    amount_due_micros: int
    status: str
    paid_at: datetime | None
    lines: list[InvoiceLine]


def _out(row: Invoice) -> dict[str, Any]:
    return {
        "id": row.pk,
        "number": row.number,
        "period_start": row.period_start,
        "period_end": row.period_end,
        "currency": row.currency,
        "plan_key": row.plan_key,
        "plan_fee_micros": row.plan_fee_micros,
        "usage_micros": row.usage_micros,
        "amount_due_micros": row.amount_due_micros,
        "status": row.status,
        "paid_at": row.paid_at,
        "lines": row.lines,
    }


def _organization_id(workspace_id: uuid.UUID) -> uuid.UUID:
    return Workspace.objects.get(pk=workspace_id).organization_id


@router.get("/invoices", response=list[InvoiceOut], operation_id="crm_invoices")
def invoices(request: HttpRequest) -> list[dict[str, Any]]:
    m = resolve_workspace(request, Permission.MANAGE_BILLING)
    return [_out(i) for i in services.list_invoices(_organization_id(m.workspace_id))]


@router.get("/invoices/{invoice_id}", response=InvoiceOut, operation_id="crm_invoice")
def invoice(request: HttpRequest, invoice_id: uuid.UUID) -> dict[str, Any]:
    m = resolve_workspace(request, Permission.MANAGE_BILLING)
    return _out(services.get_invoice(_organization_id(m.workspace_id), invoice_id))
