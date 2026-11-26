"""Connect the workspace's Meta account and see how its WhatsApp number is doing."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from audit.actors import Actor
from core.errors import NotFoundError
from crm.contacts import services as contacts
from crm.meta import services, templates
from crm.meta.models import MetaAccount, WhatsAppTemplate
from identity.authentication import api_auth
from workspaces.deps import resolve_workspace
from workspaces.permissions import Permission

router = Router(tags=["crm-meta"], auth=api_auth)


class MetaIn(Schema):
    app_secret: str
    app_id: str = ""
    whatsapp_token: str = ""
    page_token: str = ""
    waba_id: str = ""
    phone_number_id: str = ""
    page_id: str = ""
    pixel_id: str = ""


class EmbeddedSignupIn(Schema):
    code: str
    waba_id: str
    phone_number_id: str


class MetaOut(Schema):
    connected: bool
    app_id: str = ""
    waba_id: str = ""
    phone_number_id: str = ""
    display_phone: str = ""
    quality_rating: str = ""
    messaging_tier: str = ""
    status_checked_at: datetime | None = None
    page_id: str = ""
    pixel_id: str = ""
    webhook_url: str = ""
    verify_token: str = ""
    has_whatsapp_token: bool = False
    has_page_token: bool = False


def _out(account: MetaAccount) -> MetaOut:
    secret = services.secrets_of(account)
    return MetaOut(
        connected=True,
        app_id=account.app_id,
        waba_id=account.waba_id,
        phone_number_id=account.phone_number_id,
        display_phone=account.display_phone,
        quality_rating=account.quality_rating,
        messaging_tier=account.messaging_tier,
        status_checked_at=account.status_checked_at,
        page_id=account.page_id,
        pixel_id=account.pixel_id,
        webhook_url=services.webhook_url(account),
        # The verify token only proves Meta is calling the right address; it is shown so it can be
        # pasted into the Meta app. Tokens and the app secret never leave the vault.
        verify_token=secret.verify_token,
        has_whatsapp_token=bool(secret.whatsapp_token),
        has_page_token=bool(secret.page_token),
    )


@router.get("/meta/account", response=MetaOut, operation_id="crm_meta_account")
def get_account(request: HttpRequest) -> MetaOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    try:
        return _out(services.get_account(m.workspace_id))
    except NotFoundError:
        return MetaOut(connected=False)


@router.put("/meta/account", response=MetaOut, operation_id="crm_meta_connect")
def connect(request: HttpRequest, payload: MetaIn) -> MetaOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return _out(services.connect(m.workspace_id, **payload.model_dump()))


@router.post("/meta/account/status", response=MetaOut, operation_id="crm_meta_refresh_status")
def refresh_status(request: HttpRequest) -> MetaOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return _out(services.refresh_status(m.workspace_id))


@router.post("/meta/account/signup", response=MetaOut, operation_id="crm_meta_signup")
def finish_signup(request: HttpRequest, payload: EmbeddedSignupIn) -> MetaOut:
    """Finish WhatsApp embedded signup with the code the Facebook login dialog returned."""
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    account = services.finish_embedded_signup(m.workspace_id, **payload.model_dump())
    return _out(account)


@router.delete("/meta/account", response={204: None}, operation_id="crm_meta_disconnect")
def disconnect(request: HttpRequest) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    services.disconnect(m.workspace_id)
    return Status(204, None)


# ---- WhatsApp templates ----------------------------------------------------------------------


class WhatsAppTemplateIn(Schema):
    name: str
    language: str
    category: str
    body: str
    examples: list[str] = []


class WhatsAppTemplateOut(Schema):
    id: uuid.UUID
    name: str
    language: str
    category: str
    body: str
    examples: list[str]
    status: str
    reason: str
    variables: int
    synced_at: datetime | None


class TemplateSendIn(Schema):
    contact_id: uuid.UUID
    variables: list[str] = []


class TemplateSendOut(Schema):
    message_id: uuid.UUID
    blocked: bool
    reason: str


class SyncedOut(Schema):
    synced: int


def _template(row: WhatsAppTemplate) -> WhatsAppTemplateOut:
    return WhatsAppTemplateOut(
        id=row.pk,
        name=row.name,
        language=row.language,
        category=row.category,
        body=row.body,
        examples=list(row.examples),
        status=row.status,
        reason=row.reason,
        variables=len(templates.variables_of(row.body)),
        synced_at=row.synced_at,
    )


@router.get(
    "/meta/templates", response=list[WhatsAppTemplateOut], operation_id="crm_meta_templates"
)
def list_templates(request: HttpRequest) -> list[WhatsAppTemplateOut]:
    m = resolve_workspace(request, Permission.CRM_VIEW)
    return [_template(t) for t in templates.list_templates(m.workspace_id)]


@router.post(
    "/meta/templates", response={201: WhatsAppTemplateOut}, operation_id="crm_meta_template_create"
)
def create_template(
    request: HttpRequest, payload: WhatsAppTemplateIn
) -> Status[WhatsAppTemplateOut]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return Status(201, _template(templates.create_draft(m.workspace_id, **payload.model_dump())))


@router.post("/meta/templates/sync", response=SyncedOut, operation_id="crm_meta_templates_sync")
def sync_templates(request: HttpRequest) -> SyncedOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return SyncedOut(synced=templates.sync(m.workspace_id))


@router.post(
    "/meta/templates/{template_id}/submit",
    response=WhatsAppTemplateOut,
    operation_id="crm_meta_template_submit",
)
def submit_template(request: HttpRequest, template_id: uuid.UUID) -> WhatsAppTemplateOut:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    return _template(templates.submit(m.workspace_id, template_id))


@router.post(
    "/meta/templates/{template_id}/send",
    response=TemplateSendOut,
    operation_id="crm_meta_template_send",
)
def send_template(
    request: HttpRequest, template_id: uuid.UUID, payload: TemplateSendIn
) -> TemplateSendOut:
    m = resolve_workspace(request, Permission.CRM_SEND)
    contacts.get_visible_contact(m, payload.contact_id)
    outcome = templates.send(
        m.workspace_id,
        Actor.human(m.user_id),
        template_id=template_id,
        contact_id=payload.contact_id,
        variables=payload.variables,
    )
    return TemplateSendOut(
        message_id=outcome.message.pk, blocked=outcome.blocked, reason=outcome.reason
    )


@router.delete(
    "/meta/templates/{template_id}", response={204: None}, operation_id="crm_meta_template_delete"
)
def delete_template(request: HttpRequest, template_id: uuid.UUID) -> Status[None]:
    m = resolve_workspace(request, Permission.CRM_MANAGE_CHANNELS)
    templates.delete(m.workspace_id, template_id)
    return Status(204, None)
