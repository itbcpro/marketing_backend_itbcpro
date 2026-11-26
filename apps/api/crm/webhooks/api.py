"""Manage webhook endpoints (the secret is shown once, when created or rotated)."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from crm.webhooks import services
from crm.webhooks.models import WebhookDelivery, WebhookEndpoint
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-webhooks"], auth=api_auth)


class EndpointIn(Schema):
    kind: str
    name: str


class EndpointOut(Schema):
    id: uuid.UUID
    kind: str
    name: str
    public_id: str
    is_active: bool
    last_received_at: datetime | None


class CreatedOut(EndpointOut):
    secret: str


class ActiveIn(Schema):
    is_active: bool


class DeliveryOut(Schema):
    id: uuid.UUID
    status: str
    error: str
    created_at: datetime


def _out(row: WebhookEndpoint) -> EndpointOut:
    return EndpointOut(
        id=row.pk,
        kind=row.kind,
        name=row.name,
        public_id=row.public_id,
        is_active=row.is_active,
        last_received_at=row.last_received_at,
    )


def _created(created: services.Created) -> CreatedOut:
    return CreatedOut(**_out(created.endpoint).model_dump(), secret=created.secret)


def _delivery(row: WebhookDelivery) -> DeliveryOut:
    return DeliveryOut(id=row.pk, status=row.status, error=row.error, created_at=row.created_at)


@router.get("/webhooks", response=list[EndpointOut], operation_id="crm_webhooks_list")
def list_endpoints(request: HttpRequest) -> list[EndpointOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return [_out(r) for r in services.list_endpoints(membership.workspace_id)]


@router.post("/webhooks", response={201: CreatedOut}, operation_id="crm_webhooks_create")
def create(request: HttpRequest, payload: EndpointIn) -> Status[CreatedOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    created = services.create_endpoint(
        membership.workspace_id, kind=payload.kind, name=payload.name
    )
    return Status(201, _created(created))


@router.post(
    "/webhooks/{endpoint_id}/rotate", response=CreatedOut, operation_id="crm_webhooks_rotate"
)
def rotate(request: HttpRequest, endpoint_id: uuid.UUID) -> CreatedOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return _created(services.rotate_secret(membership.workspace_id, endpoint_id))


@router.patch(
    "/webhooks/{endpoint_id}", response=EndpointOut, operation_id="crm_webhooks_set_active"
)
def set_active(request: HttpRequest, endpoint_id: uuid.UUID, payload: ActiveIn) -> EndpointOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return _out(services.set_active(membership.workspace_id, endpoint_id, payload.is_active))


@router.delete("/webhooks/{endpoint_id}", response={204: None}, operation_id="crm_webhooks_delete")
def delete(request: HttpRequest, endpoint_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    services.delete_endpoint(membership.workspace_id, endpoint_id)
    return Status(204, None)


@router.get(
    "/webhooks/{endpoint_id}/deliveries",
    response=list[DeliveryOut],
    operation_id="crm_webhooks_deliveries",
)
def deliveries(request: HttpRequest, endpoint_id: uuid.UUID) -> list[DeliveryOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return [_delivery(d) for d in services.recent_deliveries(membership.workspace_id, endpoint_id)]
