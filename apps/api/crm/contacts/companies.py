"""Companies: business records contacts can belong to."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import NotFoundError, ValidationFailedError
from crm.common.normalize import InvalidValueError, normalise_email, normalise_phone, normalise_url
from crm.contacts.models import Company, Contact
from crm.fields import services as fields

OBJECT_TYPE = "company"
EDITABLE = ("name", "email", "phone", "website", "address", "description", "assigned_to")


def _clean(data: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    errors: dict[str, str] = {}
    out: dict[str, Any] = {}
    converters: dict[str, Any] = {
        "email": lambda v: normalise_email(v) if v else "",
        "phone": lambda v: normalise_phone(v) if v else "",
        "website": lambda v: normalise_url(v) if v else "",
        "name": _name,
        "address": lambda v: str(v or "").strip()[:300],
        "description": lambda v: str(v or "").strip(),
        "assigned_to": lambda v: v,
    }
    for key, convert in converters.items():
        if key in data:
            try:
                out[key] = convert(data[key])
            except (InvalidValueError, ValueError) as exc:
                errors[key] = str(exc)
    if creating and "name" not in out and "name" not in errors:
        errors["name"] = "required"
    if errors:
        raise ValidationFailedError("invalid company", errors=errors)
    return out


def _name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("required")
    if len(text) > 200:  # noqa: PLR2004 - mirrors the column length
        raise ValueError("too_long")
    return text


def get_company(workspace_id: uuid.UUID, company_id: uuid.UUID) -> Company:
    found = (
        Company.objects.for_workspace(workspace_id)
        .filter(pk=company_id, deleted_at__isnull=True)
        .first()
    )
    if found is None:
        raise NotFoundError("company not found")
    return found


def create_company(*, workspace_id: uuid.UUID, actor: Actor, data: dict[str, Any]) -> Company:
    clean = _clean(data, creating=True)
    assigned = clean.pop("assigned_to", None)
    with transaction.atomic():
        company = Company.objects.create(
            workspace_id=workspace_id, assigned_to_id=assigned, **clean
        )
        if data.get("custom"):
            fields.set_values(
                workspace_id=workspace_id,
                object_type=OBJECT_TYPE,
                record_id=company.pk,
                values=data["custom"],
            )
        record_event(
            action="crm.company.created",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_company",
            target_id=company.pk,
        )
    return company


def update_company(
    *, workspace_id: uuid.UUID, actor: Actor, company_id: uuid.UUID, data: dict[str, Any]
) -> Company:
    clean = _clean(data, creating=False)
    with transaction.atomic():
        company = get_company(workspace_id, company_id)
        for key, value in clean.items():
            setattr(company, "assigned_to_id" if key == "assigned_to" else key, value)
        company.save()
        if data.get("custom"):
            fields.set_values(
                workspace_id=workspace_id,
                object_type=OBJECT_TYPE,
                record_id=company.pk,
                values=data["custom"],
            )
        record_event(
            action="crm.company.updated",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_company",
            target_id=company.pk,
        )
    return company


def delete_company(*, workspace_id: uuid.UUID, actor: Actor, company_id: uuid.UUID) -> None:
    with transaction.atomic():
        company = get_company(workspace_id, company_id)
        company.deleted_at = timezone.now()
        company.save(update_fields=["deleted_at", "updated_at"])
        Contact.objects.filter(company=company).update(company=None)
        record_event(
            action="crm.company.deleted",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_company",
            target_id=company.pk,
        )
