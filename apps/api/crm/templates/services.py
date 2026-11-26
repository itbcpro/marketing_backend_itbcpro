"""Template use cases and rendering (public surface of the module)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.consent.models import Channel
from crm.contacts import services as contacts
from crm.fields import services as fields
from crm.fields import templating
from crm.sending import unsubscribe
from crm.templates.models import MessageTemplate, TemplateKind

NAME_LIMIT = 120
SUBJECT_LIMIT = 300
BODY_LIMIT = 50_000
CONTACT_KEYS = (
    "first_name",
    "last_name",
    "full_name",
    "email",
    "phone",
    "city",
    "state",
    "postal_code",
    "country",
    "source",
    "contact_type",
)
COMPANY_KEYS = ("name", "email", "phone", "website")
SYSTEM_KEYS = ("unsubscribe_url",)
NAMESPACES = ("contact", "company", "workspace_values", "system")


@dataclass(frozen=True)
class Rendered:
    subject: str
    body: str


def _text(value: Any) -> str:
    """How a stored value reads inside a sentence."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return f"{value.get('amount', '')} {value.get('currency', '')}".strip()
    return str(value)


def known_keys(workspace_id: uuid.UUID) -> dict[str, set[str]]:
    """Every merge field the workspace can use right now, by namespace."""
    custom = {d.key for d in fields.list_definitions(workspace_id, "contact")}
    return {
        "contact": {*CONTACT_KEYS, *custom},
        "company": set(COMPANY_KEYS),
        "workspace_values": set(fields.workspace_values(workspace_id)),
        "system": set(SYSTEM_KEYS),
    }


def check_references(workspace_id: uuid.UUID, *texts: str) -> dict[str, str]:
    """Merge fields used in the texts that do not exist, as ``{"ns.key": "unknown_key"}``."""
    known = known_keys(workspace_id)
    problems: dict[str, str] = {}
    for text in texts:
        for namespace, key in templating.referenced_keys(text):
            if key not in known.get(namespace, set()):
                problems[f"{namespace}.{key}"] = (
                    "unknown_namespace" if namespace not in known else "unknown_key"
                )
    return problems


def build_context(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    contact = contacts.get_contact(workspace_id, contact_id)
    item = contacts.serialise_contacts(workspace_id, [contact])[0]
    company: dict[str, Any] = {}
    if contact.company_id:
        found = contacts.get_company(workspace_id, contact.company_id)
        company = {key: getattr(found, key) for key in COMPANY_KEYS}
    return {
        "contact": {
            **{key: _text(item.get(key)) for key in CONTACT_KEYS},
            **{key: _text(value) for key, value in item["custom"].items()},
        },
        "company": company,
        "workspace_values": fields.workspace_values(workspace_id),
        "system": {"unsubscribe_url": unsubscribe.page_url_for(workspace_id, contact_id)},
    }


def render_text(
    workspace_id: uuid.UUID, contact_id: uuid.UUID, *, subject: str, body: str
) -> Rendered:
    context = build_context(workspace_id, contact_id)
    return Rendered(templating.render(subject, context), templating.render(body, context))


def _clean(
    workspace_id: uuid.UUID, *, name: str, kind: str, channel: str, subject: str, body: str
) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not name.strip() or len(name.strip()) > NAME_LIMIT:
        errors["name"] = "required" if not name.strip() else "too_long"
    if kind not in TemplateKind.values:
        errors["kind"] = "invalid_value"
    if channel and channel not in Channel.values:
        errors["channel"] = "invalid_value"
    if len(subject) > SUBJECT_LIMIT:
        errors["subject"] = "too_long"
    if not body.strip() or len(body) > BODY_LIMIT:
        errors["body"] = "required" if not body.strip() else "too_long"
    if errors:
        raise ValidationFailedError("invalid template", errors=errors)
    problems = check_references(workspace_id, subject, body)
    if problems:
        raise ValidationFailedError("unknown merge fields", errors=problems)
    return {
        "name": name.strip(),
        "kind": kind,
        "channel": channel,
        "subject": subject.strip(),
        "body": body,
    }


def create(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    name: str,
    body: str,
    kind: str = TemplateKind.TEMPLATE,
    channel: str = "",
    subject: str = "",
) -> MessageTemplate:
    clean = _clean(workspace_id, name=name, kind=kind, channel=channel, subject=subject, body=body)
    try:
        with transaction.atomic():
            row = MessageTemplate.objects.create(
                workspace_id=workspace_id,
                created_by_id=uuid.UUID(actor.id) if actor.type == "human" else None,
                **clean,
            )
            record_event(
                action="crm.template.created",
                actor=actor,
                workspace_id=workspace_id,
                target_type="crm_template",
                target_id=row.pk,
            )
    except IntegrityError as exc:
        raise ConflictError("template name exists", errors={"name": "duplicate"}) from exc
    return row


def get(workspace_id: uuid.UUID, template_id: uuid.UUID) -> MessageTemplate:
    found = MessageTemplate.objects.for_workspace(workspace_id).filter(pk=template_id).first()
    if found is None:
        raise NotFoundError("template not found")
    return found


def update(
    *, workspace_id: uuid.UUID, actor: Actor, template_id: uuid.UUID, data: dict[str, Any]
) -> MessageTemplate:
    row = get(workspace_id, template_id)
    merged = {
        "name": row.name,
        "kind": row.kind,
        "channel": row.channel,
        "subject": row.subject,
        "body": row.body,
        **data,
    }
    clean = _clean(workspace_id, **merged)
    try:
        with transaction.atomic():
            for key, value in clean.items():
                setattr(row, key, value)
            row.save()
            record_event(
                action="crm.template.updated",
                actor=actor,
                workspace_id=workspace_id,
                target_type="crm_template",
                target_id=row.pk,
            )
    except IntegrityError as exc:
        raise ConflictError("template name exists", errors={"name": "duplicate"}) from exc
    return row


def archive(*, workspace_id: uuid.UUID, actor: Actor, template_id: uuid.UUID) -> None:
    row = get(workspace_id, template_id)
    row.archived_at = timezone.now()
    row.save(update_fields=["archived_at", "updated_at"])
    record_event(
        action="crm.template.archived",
        actor=actor,
        workspace_id=workspace_id,
        target_type="crm_template",
        target_id=row.pk,
    )


def list_templates(
    workspace_id: uuid.UUID, *, kind: str | None = None, channel: str | None = None
) -> list[MessageTemplate]:
    rows = MessageTemplate.objects.for_workspace(workspace_id).filter(archived_at__isnull=True)
    if kind:
        rows = rows.filter(kind=kind)
    if channel:
        rows = rows.filter(channel__in=[channel, ""])
    return list(rows)


def render(*, workspace_id: uuid.UUID, template_id: uuid.UUID, contact_id: uuid.UUID) -> Rendered:
    row = get(workspace_id, template_id)
    return render_text(workspace_id, contact_id, subject=row.subject, body=row.body)
