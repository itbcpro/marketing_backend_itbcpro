"""WhatsApp message templates: draft, submit for approval, sync the verdict, and send."""

from __future__ import annotations

import re
import uuid
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.actors import Actor
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.consent.models import Channel, Purpose
from crm.meta import services
from crm.meta.graph import GraphError
from crm.meta.models import TemplateCategory, TemplateStatus, WhatsAppTemplate
from crm.sending import services as sending

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
VARIABLE = re.compile(r"\{\{(\d+)\}\}")
BODY_LIMIT = 1024
PAGE_SIZE = 100
MAX_PAGES = 20
# Meta's names for template states, mapped to ours (anything unlisted is left as it was).
META_STATUS = {
    "APPROVED": TemplateStatus.APPROVED,
    "PENDING": TemplateStatus.PENDING,
    "REJECTED": TemplateStatus.REJECTED,
    "PAUSED": TemplateStatus.PAUSED,
    "DISABLED": TemplateStatus.DISABLED,
}


def variables_of(body: str) -> list[int]:
    return [int(n) for n in VARIABLE.findall(body)]


def _check(name: str, language: str, category: str, body: str, examples: list[str]) -> None:
    if not NAME_PATTERN.match(name) or not LANGUAGE_PATTERN.match(language):
        raise ValidationFailedError("invalid_template_name")
    if category not in TemplateCategory.values:
        raise ValidationFailedError("invalid_category")
    if not body.strip() or len(body) > BODY_LIMIT:
        raise ValidationFailedError("invalid_body")
    numbers = variables_of(body)
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValidationFailedError("variables_must_be_numbered_in_order")
    if len(examples) != len(numbers) or not all(e.strip() for e in examples):
        raise ValidationFailedError("one_example_per_variable")


def list_templates(workspace_id: uuid.UUID) -> list[WhatsAppTemplate]:
    return list(WhatsAppTemplate.objects.for_workspace(workspace_id))


def get_template(workspace_id: uuid.UUID, template_id: uuid.UUID) -> WhatsAppTemplate:
    found = WhatsAppTemplate.objects.for_workspace(workspace_id).filter(pk=template_id).first()
    if found is None:
        raise NotFoundError("template not found")
    return found


def create_draft(
    workspace_id: uuid.UUID,
    *,
    name: str,
    language: str,
    category: str,
    body: str,
    examples: list[str],
) -> WhatsAppTemplate:
    _check(name, language, category, body, examples)
    try:
        with transaction.atomic():
            return WhatsAppTemplate.objects.create(
                workspace_id=workspace_id,
                name=name,
                language=language,
                category=category,
                body=body,
                examples=examples,
            )
    except IntegrityError as exc:
        raise ConflictError("template already exists") from exc


def submit(workspace_id: uuid.UUID, template_id: uuid.UUID) -> WhatsAppTemplate:
    """Send a draft (or a rejected template, after edits) to Meta for review."""
    template = get_template(workspace_id, template_id)
    if template.status not in (TemplateStatus.DRAFT, TemplateStatus.REJECTED):
        raise ConflictError("template already submitted")
    account = services.get_account(workspace_id)
    if not account.waba_id:
        raise ValidationFailedError("no_whatsapp_account")
    body: dict[str, Any] = {
        "name": template.name,
        "language": template.language,
        "category": template.category.upper(),
        "components": [{"type": "BODY", "text": template.body}],
    }
    if template.examples:
        body["components"][0]["example"] = {"body_text": [template.examples]}
    try:
        data = services.client(services.secrets_of(account).whatsapp_token).post(
            f"/{account.waba_id}/message_templates", body
        )
    except GraphError as exc:
        raise ValidationFailedError(exc.code) from exc
    template.meta_template_id = str(data.get("id", ""))
    template.status = META_STATUS.get(str(data.get("status", "PENDING")), TemplateStatus.PENDING)
    template.reason = ""
    template.save()
    return template


def delete(workspace_id: uuid.UUID, template_id: uuid.UUID) -> None:
    template = get_template(workspace_id, template_id)
    if template.meta_template_id:
        account = services.get_account(workspace_id)
        try:
            services.client(services.secrets_of(account).whatsapp_token).delete(
                f"/{account.waba_id}/message_templates", {"name": template.name}
            )
        except GraphError as exc:
            raise ValidationFailedError(exc.code) from exc
    template.delete()


def _body_text(components: list[dict[str, Any]]) -> str:
    return next((str(c.get("text", "")) for c in components if c.get("type") == "BODY"), "")


def apply_verdict(
    workspace_id: uuid.UUID, *, meta_id: str, name: str, status: str, reason: str = ""
) -> None:
    """Record Meta's decision about a template, from a webhook or from the periodic sync."""
    mapped = META_STATUS.get(status.upper())
    if mapped is None:
        return
    rows = WhatsAppTemplate.objects.for_workspace(workspace_id)
    row = rows.filter(meta_template_id=meta_id).first() if meta_id else None
    row = row or rows.filter(name=name).first()
    if row is None:
        return
    row.status = mapped
    row.reason = reason[:300]
    row.meta_template_id = meta_id or row.meta_template_id
    row.synced_at = timezone.now()
    row.save()


def sync(workspace_id: uuid.UUID) -> int:
    """Pull every template of the account from Meta; unknown ones are added. Returns the count."""
    account = services.get_account(workspace_id)
    if not account.waba_id:
        raise ValidationFailedError("no_whatsapp_account")
    client = services.client(services.secrets_of(account).whatsapp_token)
    params: dict[str, Any] = {
        "fields": "id,name,language,status,category,components,rejected_reason",
        "limit": PAGE_SIZE,
    }
    seen = 0
    try:
        for _ in range(MAX_PAGES):
            page = client.get(f"/{account.waba_id}/message_templates", params)
            for item in page.get("data", []):
                _upsert(workspace_id, item)
                seen += 1
            after = page.get("paging", {}).get("cursors", {}).get("after")
            if not page.get("paging", {}).get("next") or not after:
                break
            params["after"] = after
    except GraphError as exc:
        raise ValidationFailedError(exc.code) from exc
    return seen


def _upsert(workspace_id: uuid.UUID, item: dict[str, Any]) -> None:
    status = META_STATUS.get(str(item.get("status", "")).upper())
    category = str(item.get("category", "")).lower()
    if status is None or category not in TemplateCategory.values:
        return
    WhatsAppTemplate.objects.update_or_create(
        workspace_id=workspace_id,
        name=str(item["name"]),
        language=str(item["language"]),
        defaults={
            "category": category,
            "status": status,
            "meta_template_id": str(item.get("id", "")),
            "reason": str(item.get("rejected_reason") or "")[:300],
            "synced_at": timezone.now(),
            "body": _body_text(item.get("components", [])),
        },
    )


def render_preview(template: WhatsAppTemplate, variables: list[str]) -> str:
    return VARIABLE.sub(lambda m: variables[int(m.group(1)) - 1], template.body)


def send(
    workspace_id: uuid.UUID,
    actor: Actor,
    *,
    template_id: uuid.UUID,
    contact_id: uuid.UUID,
    variables: list[str],
) -> sending.SendOutcome:
    """Send an approved template through the gateway; it may open a new conversation."""
    template = get_template(workspace_id, template_id)
    if template.status != TemplateStatus.APPROVED:
        raise ConflictError("template is not approved")
    if len(variables) != len(variables_of(template.body)):
        raise ValidationFailedError("wrong_number_of_variables")
    return sending.send(
        workspace_id=workspace_id,
        actor=actor,
        contact_id=contact_id,
        channel=Channel.WHATSAPP,
        body=render_preview(template, variables),
        purpose=Purpose.TRANSACTIONAL
        if template.category != TemplateCategory.MARKETING
        else Purpose.MARKETING,
        attachments=[
            {
                "kind": "template",
                "name": template.name,
                "language": template.language,
                "category": template.category,
                "variables": variables,
            }
        ],
    )
