"""Connect an SMS provider account, manage sender numbers and track their registration."""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router, Schema, Status

from core.errors import NotFoundError, ValidationFailedError
from crm.common.normalize import InvalidValueError, normalise_phone
from crm.consent.models import Channel
from crm.inbox import services as inbox
from crm.inbox.models import ChannelAccount
from crm.sms import registration, services
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-sms"], auth=api_auth)
PROVIDER = "twilio"


class SmsAccountIn(Schema):
    account_sid: str
    auth_token: str


class SmsAccountOut(Schema):
    connected: bool
    account_sid: str = ""
    status_callback_url: str = ""
    inbound_url: str = ""


class SmsNumberIn(Schema):
    number: str
    label: str = ""


class SmsRegistrationIn(Schema):
    status: str
    completed_steps: list[str] = []


class SmsNumberOut(Schema):
    id: uuid.UUID
    number: str
    label: str
    registration_status: str
    completed_steps: list[str]


class SmsStepOut(Schema):
    key: str
    required: bool


class SmsRegistrationOut(Schema):
    country: str
    steps: list[SmsStepOut]
    statuses: list[str]


def _account_out(workspace_id: uuid.UUID) -> SmsAccountOut:
    try:
        row = services.get_credential(workspace_id)
    except NotFoundError:
        return SmsAccountOut(connected=False)
    return SmsAccountOut(
        connected=True,
        account_sid=row.account_sid,
        status_callback_url=services.callback_url(row, "status"),
        inbound_url=services.callback_url(row, "inbound"),
    )


def _number(row: ChannelAccount) -> SmsNumberOut:
    return SmsNumberOut(
        id=row.pk,
        number=row.external_id,
        label=row.display_name,
        registration_status=row.config.get("registration", registration.Status.NOT_STARTED),
        completed_steps=list(row.config.get("completed_steps", [])),
    )


@router.get("/sms/account", response=SmsAccountOut, operation_id="crm_sms_account")
def get_account(request: HttpRequest) -> SmsAccountOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return _account_out(m.workspace_id)


@router.put("/sms/account", response=SmsAccountOut, operation_id="crm_sms_connect")
def connect(request: HttpRequest, payload: SmsAccountIn) -> SmsAccountOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    services.connect(m.workspace_id, account_sid=payload.account_sid, auth_token=payload.auth_token)
    return _account_out(m.workspace_id)


@router.delete("/sms/account", response={204: None}, operation_id="crm_sms_disconnect")
def disconnect(request: HttpRequest) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    services.disconnect(m.workspace_id)
    return Status(204, None)


@router.get("/sms/numbers", response=list[SmsNumberOut], operation_id="crm_sms_numbers")
def numbers(request: HttpRequest) -> list[SmsNumberOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return [_number(r) for r in inbox.list_accounts(m.workspace_id, Channel.SMS)]


@router.post("/sms/numbers", response={201: SmsNumberOut}, operation_id="crm_sms_number_add")
def add_number(request: HttpRequest, payload: SmsNumberIn) -> Status[SmsNumberOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    try:
        e164 = normalise_phone(payload.number)
    except InvalidValueError as exc:
        raise ValidationFailedError("invalid number", errors={"number": str(exc)}) from exc
    row = inbox.add_account(
        m.workspace_id,
        channel=Channel.SMS,
        provider=PROVIDER,
        external_id=e164,
        display_name=payload.label,
        config={"registration": registration.Status.NOT_STARTED, "completed_steps": []},
    )
    return Status(201, _number(row))


@router.put(
    "/sms/numbers/{number_id}/registration",
    response=SmsNumberOut,
    operation_id="crm_sms_number_registration",
)
def set_registration(
    request: HttpRequest, number_id: uuid.UUID, payload: SmsRegistrationIn
) -> SmsNumberOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    known = {s.key for s in registration.steps_for(registration.DEFAULT_COUNTRY)} | {
        s.key for steps in registration.STEPS.values() for s in steps
    }
    if payload.status not in registration.STATUSES or not set(payload.completed_steps) <= known:
        raise ValidationFailedError("invalid registration")
    row = inbox.update_account_config(
        m.workspace_id,
        number_id,
        {"registration": payload.status, "completed_steps": payload.completed_steps},
    )
    return _number(row)


@router.delete(
    "/sms/numbers/{number_id}", response={204: None}, operation_id="crm_sms_number_remove"
)
def remove_number(request: HttpRequest, number_id: uuid.UUID) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    inbox.remove_account(m.workspace_id, number_id)
    return Status(204, None)


@router.get("/sms/registration", response=SmsRegistrationOut, operation_id="crm_sms_registration")
def registration_steps(
    request: HttpRequest, country: str = registration.DEFAULT_COUNTRY
) -> SmsRegistrationOut:
    resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    steps = registration.steps_for(country)
    return SmsRegistrationOut(
        country=country.upper()
        if country.upper() in registration.STEPS
        else registration.DEFAULT_COUNTRY,
        steps=[SmsStepOut(key=s.key, required=s.required) for s in steps],
        statuses=list(registration.STATUSES),
    )
