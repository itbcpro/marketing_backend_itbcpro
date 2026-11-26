"""Send a message from the inbox."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.errors import NotFoundError
from crm.consent.models import Purpose
from crm.inbox.selectors import visible
from crm.sending import domains, registry, suppression
from crm.sending.gateway import send
from crm.sending.models import DomainRecord, SendingDomain, Suppression
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-sending"], auth=api_auth)


class SendIn(Schema):
    channel: str
    body: str
    subject: str = ""
    purpose: str = Purpose.TRANSACTIONAL
    idempotency_key: str = ""


class SendOut(Schema):
    message_id: uuid.UUID
    status: str
    blocked: bool
    reason: str
    created_at: datetime


@router.post(
    "/inbox/conversations/{conversation_id}/send",
    response={201: SendOut},
    operation_id="crm_inbox_send",
)
def send_reply(
    request: HttpRequest, conversation_id: uuid.UUID, payload: SendIn
) -> Status[SendOut]:
    """Reply on one channel. A blocked reply is stored and returned with its reason (201)."""
    membership = resolve_workspace(request, Permission.CRM_SEND)
    conversation = visible(membership).filter(pk=conversation_id).first()
    if conversation is None:
        raise NotFoundError("conversation not found")
    outcome = send(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        contact_id=conversation.contact_id,
        **payload.model_dump(),
    )
    message = outcome.message
    return Status(
        201,
        SendOut(
            message_id=message.pk,
            status=message.status,
            blocked=outcome.blocked,
            reason=outcome.reason,
            created_at=message.created_at,
        ),
    )


@router.get("/inbox/channels", response=list[str], operation_id="crm_inbox_channels")
def available_channels(request: HttpRequest) -> list[str]:
    """Channels that can currently send in this deployment."""
    resolve_workspace(request, Permission.CRM_VIEW)
    return registry.channels()


class SuppressionIn(Schema):
    channel: str = "email"
    address: str
    note: str = ""


class SuppressionOut(Schema):
    id: uuid.UUID
    channel: str
    address: str
    reason: str
    note: str
    created_at: datetime


@router.get("/suppressions", response=list[SuppressionOut], operation_id="crm_suppressions_list")
def list_suppressions(request: HttpRequest) -> list[SuppressionOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = Suppression.objects.for_workspace(membership.workspace_id).order_by("-created_at")
    return [SuppressionOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/suppressions", response={201: SuppressionOut}, operation_id="crm_suppressions_add")
def add_suppression(request: HttpRequest, payload: SuppressionIn) -> Status[SuppressionOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    row, _ = suppression.suppress(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        channel=payload.channel,
        address=payload.address,
        reason="manual",
        note=payload.note,
    )
    return Status(201, SuppressionOut.model_validate(row, from_attributes=True))


@router.delete(
    "/suppressions/{suppression_id}", response={204: None}, operation_id="crm_suppressions_remove"
)
def remove_suppression(request: HttpRequest, suppression_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_SETTINGS)
    suppression.remove(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        suppression_id=suppression_id,
    )
    return Status(204, None)


class DomainIn(Schema):
    domain: str


class RecordOut(Schema):
    id: uuid.UUID
    purpose: str
    record_type: str
    name: str
    value: str
    priority: int | None
    required: bool
    status: str
    observed: str
    checked_at: datetime | None


class DomainOut(Schema):
    id: uuid.UUID
    domain: str
    status: str
    verified_at: datetime | None
    last_checked_at: datetime | None
    records: list[RecordOut]


def _domain_out(row: SendingDomain) -> DomainOut:
    records = DomainRecord.objects.filter(domain=row)
    return DomainOut(
        id=row.pk,
        domain=row.domain,
        status=row.status,
        verified_at=row.verified_at,
        last_checked_at=row.last_checked_at,
        records=[RecordOut.model_validate(r, from_attributes=True) for r in records],
    )


@router.get("/email/domains", response=list[DomainOut], operation_id="crm_domains_list")
def list_domains(request: HttpRequest) -> list[DomainOut]:
    membership = resolve_workspace(request, Permission.CRM_VIEW)
    rows = SendingDomain.objects.for_workspace(membership.workspace_id).order_by("domain")
    return [_domain_out(r) for r in rows]


@router.post("/email/domains", response={201: DomainOut}, operation_id="crm_domains_add")
def add_domain(request: HttpRequest, payload: DomainIn) -> Status[DomainOut]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    row = domains.add_domain(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        domain=payload.domain,
    )
    return Status(201, _domain_out(row))


@router.post(
    "/email/domains/{domain_id}/verify", response=DomainOut, operation_id="crm_domains_verify"
)
def verify_domain(request: HttpRequest, domain_id: uuid.UUID) -> DomainOut:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    row = domains.verify(workspace_id=membership.workspace_id, domain_id=domain_id)
    return _domain_out(row)


@router.delete(
    "/email/domains/{domain_id}", response={204: None}, operation_id="crm_domains_remove"
)
def remove_domain(request: HttpRequest, domain_id: uuid.UUID) -> Status[None]:
    membership = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    domains.delete_domain(
        workspace_id=membership.workspace_id,
        actor=Actor.human(membership.user_id),
        domain_id=domain_id,
    )
    return Status(204, None)
