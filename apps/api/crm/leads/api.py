"""Lead ad forms, captured leads and Conversions API rules."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Router, Schema, Status

from crm.leads import capi, services
from crm.leads.models import ConversionRule, LeadCapture, LeadFormMapping
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission
from workspaces.scope import restrict

router = Router(tags=["crm-leads"], auth=api_auth)


class LeadFormIn(Schema):
    form_id: str
    name: str = ""
    field_map: dict[str, str] = {}
    tags: list[str] = []
    consent_channels: list[str] = []


class LeadFormOut(Schema):
    id: uuid.UUID
    form_id: str
    name: str
    field_map: dict[str, str]
    tags: list[str]
    consent_channels: list[str]


class LeadOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str
    form_id: str
    ad_id: str
    campaign_id: str
    answers: dict[str, str]
    created_at: datetime


class ConversionRuleIn(Schema):
    name: str
    trigger: str
    event_name: str
    stage_id: uuid.UUID | None = None
    send_value: bool = False


class ConversionRuleOut(ConversionRuleIn):
    id: uuid.UUID
    is_active: bool


class RuleActiveIn(Schema):
    is_active: bool


class ConversionLogOut(Schema):
    id: uuid.UUID
    rule_name: str
    outcome: str
    detail: str
    created_at: datetime


def _form(row: LeadFormMapping) -> LeadFormOut:
    return LeadFormOut(
        id=row.pk,
        form_id=row.form_id,
        name=row.name,
        field_map=dict(row.mapping),
        tags=list(row.tags),
        consent_channels=list(row.consent_channels),
    )


def _lead(row: LeadCapture) -> LeadOut:
    contact = row.contact
    return LeadOut(
        id=row.pk,
        contact_id=row.contact_id,
        contact_name=contact.full_name or contact.email or contact.phone,
        form_id=row.form_id,
        ad_id=row.ad_id,
        campaign_id=row.campaign_id,
        answers={k: str(v) for k, v in row.answers.items()},
        created_at=row.created_at,
    )


def _rule(row: ConversionRule) -> ConversionRuleOut:
    return ConversionRuleOut(
        id=row.pk,
        name=row.name,
        trigger=row.trigger,
        event_name=row.event_name,
        stage_id=row.stage_id,
        send_value=row.send_value,
        is_active=row.is_active,
    )


@router.get("/leads/forms", response=list[LeadFormOut], operation_id="crm_lead_forms")
def forms(request: HttpRequest) -> list[LeadFormOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return [_form(r) for r in services.list_mappings(m.workspace_id)]


@router.put("/leads/forms", response=LeadFormOut, operation_id="crm_lead_form_save")
def save_form(request: HttpRequest, payload: LeadFormIn) -> LeadFormOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    row = services.save_mapping(
        m.workspace_id,
        form_id=payload.form_id,
        name=payload.name,
        field_map=payload.field_map,
        tags=payload.tags,
        consent_channels=payload.consent_channels,
    )
    return _form(row)


@router.delete(
    "/leads/forms/{mapping_id}", response={204: None}, operation_id="crm_lead_form_delete"
)
def delete_form(request: HttpRequest, mapping_id: uuid.UUID) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    services.delete_mapping(m.workspace_id, mapping_id)
    return Status(204, None)


@router.get("/leads", response=list[LeadOut], operation_id="crm_leads")
def leads(request: HttpRequest, limit: int = 50) -> list[LeadOut]:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    rows: QuerySet[LeadCapture] = LeadCapture.objects.for_workspace(m.workspace_id).select_related(
        "contact"
    )
    rows = restrict(rows, m, owner_field="contact__assigned_to_id")
    return [_lead(r) for r in rows[: max(1, min(limit, 200))]]


@router.get(
    "/leads/conversion-rules", response=list[ConversionRuleOut], operation_id="crm_conversion_rules"
)
def rules(request: HttpRequest) -> list[ConversionRuleOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return [_rule(r) for r in capi.list_rules(m.workspace_id)]


@router.post(
    "/leads/conversion-rules",
    response={201: ConversionRuleOut},
    operation_id="crm_conversion_rule_create",
)
def create_rule(request: HttpRequest, payload: ConversionRuleIn) -> Status[ConversionRuleOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return Status(201, _rule(capi.create_rule(m.workspace_id, **payload.model_dump())))


@router.patch(
    "/leads/conversion-rules/{rule_id}",
    response=ConversionRuleOut,
    operation_id="crm_conversion_rule_active",
)
def set_rule_active(
    request: HttpRequest, rule_id: uuid.UUID, payload: RuleActiveIn
) -> ConversionRuleOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return _rule(capi.set_active(m.workspace_id, rule_id, payload.is_active))


@router.delete(
    "/leads/conversion-rules/{rule_id}",
    response={204: None},
    operation_id="crm_conversion_rule_delete",
)
def delete_rule(request: HttpRequest, rule_id: uuid.UUID) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    capi.delete_rule(m.workspace_id, rule_id)
    return Status(204, None)


@router.get(
    "/leads/conversions", response=list[ConversionLogOut], operation_id="crm_conversion_log"
)
def conversion_log(request: HttpRequest) -> list[ConversionLogOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return [
        ConversionLogOut(
            id=r.pk,
            rule_name=r.rule.name,
            outcome=r.outcome,
            detail=r.detail,
            created_at=r.created_at,
        )
        for r in capi.recent_logs(m.workspace_id)
    ]
