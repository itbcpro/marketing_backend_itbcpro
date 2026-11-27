"""Lead intake: a POST from Zapier, n8n, a form or any tool becomes a contact and a deal.

The body is JSON or a form. Field names are forgiving (``email``, ``Email``, ``Phone Number``...).
Where the lead goes comes from the endpoint's ``config`` (pipeline stage, owner, tags, source); a
field in the request (``pipeline``, ``stage``, ``owner``) overrides it. A person who is already a
contact is not overwritten: they only gain tags, a note and, when asked, a deal.
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import parse_qsl

from audit.actors import Actor
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.activity import services as activity
from crm.contacts import services as contacts
from crm.pipelines import services as pipelines
from crm.webhooks.models import WebhookEndpoint
from workspaces.models import Membership

ACTOR = Actor.system("lead_intake")
MICROS = 1_000_000
SOURCE_LIMIT = 100
ALIASES = {
    "firstname": "first_name",
    "lastname": "last_name",
    "name": "name",
    "fullname": "name",
    "email": "email",
    "emailaddress": "email",
    "phone": "phone",
    "phonenumber": "phone",
    "mobile": "phone",
    "telephone": "phone",
    "address": "address",
    "city": "city",
    "state": "state",
    "province": "state",
    "postalcode": "postal_code",
    "postcode": "postal_code",
    "zip": "postal_code",
    "country": "country",
    "company": "company",
    "companyname": "company",
    "businessname": "company",
    "tags": "tags",
    "tag": "tags",
    "source": "source",
    "notes": "notes",
    "note": "notes",
    "message": "notes",
    "pipeline": "pipeline",
    "stage": "stage",
    "owner": "owner",
    "assignto": "owner",
    "assigntouser": "owner",
    "assignedto": "owner",
    "value": "value",
    "amount": "value",
    "leadvalue": "value",
    "dealname": "deal_name",
    "opportunityname": "deal_name",
}
CONTACT_FIELDS = (
    "first_name",
    "last_name",
    "address",
    "city",
    "state",
    "postal_code",
    "country",
)


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def parse_payload(body: bytes) -> dict[str, Any]:
    """The request body as a flat dict: JSON object, or a url-encoded form."""
    try:
        data = json.loads(body)
    except ValueError:
        data = dict(parse_qsl(body.decode("utf-8", "replace")))
    if not isinstance(data, dict) or not data:
        raise ValidationFailedError("invalid payload", errors={"body": "invalid_payload"})
    return data


def _text(value: Any) -> str:
    if value is None or isinstance(value, dict | list):
        return ""
    return " ".join(str(value).split())


def _fields(data: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Known fields by their canonical name, and "label: value" lines for everything else."""
    known: dict[str, str] = {}
    extra: list[str] = []
    for label, raw in data.items():
        canonical = ALIASES.get(_key(str(label)))
        if isinstance(raw, dict):  # a nested ``extra`` object: each entry is one line
            extra.extend(f"{k}: {_text(v)}" for k, v in raw.items() if _text(v))
        elif canonical and _text(raw) and canonical not in known:
            known[canonical] = str(raw).strip() if canonical == "notes" else _text(raw)
        elif not canonical and _text(raw):
            extra.append(f"{label}: {_text(raw)}")
    return known, extra


def clean_config(workspace_id: uuid.UUID, config: dict[str, Any]) -> dict[str, Any]:
    """Validate the settings of a lead source and keep only the known keys."""
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if config.get("stage_id"):
        try:
            stage = pipelines.get_stage(workspace_id, uuid.UUID(str(config["stage_id"])))
            out["stage_id"] = str(stage.pk)
        except NotFoundError, ValueError:
            errors["stage_id"] = "unknown_stage"
    if config.get("owner_id"):
        owner = _member(workspace_id, str(config["owner_id"]))
        if owner is None:
            errors["owner_id"] = "not_a_member"
        else:
            out["owner_id"] = str(owner)
    out["source"] = _text(config.get("source"))[:SOURCE_LIMIT]
    tags = config.get("tags") or []
    out["tags"] = [t for t in (_text(x) for x in tags) if t] if isinstance(tags, list) else []
    if errors:
        raise ValidationFailedError("invalid config", errors=errors)
    return out


def _member(workspace_id: uuid.UUID, text: str) -> uuid.UUID | None:
    try:
        wanted = uuid.UUID(text)
    except ValueError:
        return pipelines.resolve_owner(workspace_id, text)
    ok = Membership.objects.filter(workspace_id=workspace_id, user_id=wanted).exists()
    return wanted if ok else None


def _identity(workspace_id: uuid.UUID, fields: dict[str, str]) -> tuple[dict[str, Any], str]:
    """Email and phone as the CRM stores them. An invalid phone is dropped, not fatal."""
    wanted: dict[str, Any] = {k: fields[k] for k in ("email", "phone") if k in fields}
    dropped = ""
    for _ in range(2):
        try:
            return contacts.validate_contact_data(workspace_id, wanted), dropped
        except ValidationFailedError as exc:
            problems = cast("dict[str, Any]", exc.params.get("errors", {}))
            if "phone" in problems and "phone" in wanted and wanted.get("email"):
                dropped = wanted.pop("phone")
                continue
            raise ValidationFailedError(
                "invalid lead", errors={k: str(v) for k, v in problems.items()}
            ) from exc
    return wanted, dropped


def _contact(
    workspace_id: uuid.UUID,
    fields: dict[str, str],
    identity: dict[str, Any],
    config: dict[str, Any],
    owner: uuid.UUID | None,
) -> tuple[Any, bool]:
    for channel in ("email", "phone"):
        value = identity.get(channel)
        found = contacts.find_contact(workspace_id, channel, value) if value else None
        if found is not None:
            return found, False
    data: dict[str, Any] = {k: fields[k] for k in CONTACT_FIELDS if k in fields}
    data.update(identity)
    if "first_name" not in data and fields.get("name"):
        first, _, last = fields["name"].partition(" ")
        data.update(first_name=first, last_name=last.strip())
    data["source"] = (fields.get("source") or config.get("source") or "")[:SOURCE_LIMIT]
    if owner:
        data["assigned_to"] = owner
    if fields.get("company"):
        data["company_id"] = contacts.find_or_create_company(
            workspace_id=workspace_id, actor=ACTOR, name=fields["company"]
        ).pk
    return contacts.create_contact(workspace_id=workspace_id, actor=ACTOR, data=data), True


def _stage(workspace_id: uuid.UUID, fields: dict[str, str], config: dict[str, Any]) -> Any:
    if fields.get("pipeline"):
        return pipelines.resolve_stage(
            workspace_id,
            pipeline=fields["pipeline"],
            stage=fields.get("stage", ""),
            create_missing=False,
        )
    if config.get("stage_id"):
        return pipelines.get_stage(workspace_id, uuid.UUID(config["stage_id"]))
    return None


def _value(text: str) -> int:
    try:
        amount = Decimal(text.replace(",", "").lstrip("$"))
    except InvalidOperation as exc:
        raise ValidationFailedError("invalid lead", errors={"value": "invalid_number"}) from exc
    if amount < 0:
        raise ValidationFailedError("invalid lead", errors={"value": "invalid_number"})
    return int(amount * MICROS)


def handle(endpoint: WebhookEndpoint, body: bytes) -> None:
    """Take one lead; every way it can be refused is a ValidationFailedError."""
    try:
        _handle(endpoint, body)
    except (NotFoundError, ConflictError) as exc:
        raise ValidationFailedError("invalid lead", errors={"lead": str(exc)}) from exc


def _handle(endpoint: WebhookEndpoint, body: bytes) -> None:
    workspace_id, config = endpoint.workspace_id, endpoint.config
    fields, extra = _fields(parse_payload(body))
    identity, bad_phone = _identity(workspace_id, fields)
    if not identity:
        raise ValidationFailedError("invalid lead", errors={"email": "email_or_phone_required"})
    owner = pipelines.resolve_owner(workspace_id, fields.get("owner", "")) or (
        uuid.UUID(config["owner_id"]) if config.get("owner_id") else None
    )
    stage = _stage(workspace_id, fields, config)  # before any write: a bad pipeline fails whole
    contact, _ = _contact(workspace_id, fields, identity, config, owner)
    tags = [*config.get("tags", []), *re.split(r"[;,]", fields.get("tags", ""))]
    tags = [t.strip() for t in tags if t.strip()]
    if tags:
        contacts.add_tags(workspace_id=workspace_id, actor=ACTOR, contact=contact, names=tags)
    deal = None
    if stage is not None:
        who = fields.get("name") or " ".join(
            x for x in (fields.get("first_name"), fields.get("last_name")) if x
        )
        name = fields.get("deal_name") or who or identity.get("email") or identity["phone"]
        deal = pipelines.find_deal(
            workspace_id, contact_id=contact.pk, pipeline_id=stage.pipeline_id, name=name
        )
        if deal is None:
            deal = pipelines.create_opportunity(
                workspace_id,
                ACTOR,
                contact_id=contact.pk,
                pipeline_id=stage.pipeline_id,
                stage_id=stage.pk,
                name=name,
                value_micros=_value(fields["value"]) if fields.get("value") else 0,
                assigned_to=owner,
                source=(fields.get("source") or config.get("source") or "")[:SOURCE_LIMIT],
                tags=tags,
            )
    lines = [fields["notes"]] if fields.get("notes") else []
    lines += extra
    if bad_phone:
        lines.append(f"phone (not a valid number): {bad_phone}")
    if lines:
        activity.add_note(
            workspace_id=workspace_id,
            actor=ACTOR,
            contact_id=contact.pk,
            body="\n".join(lines)[:19_000],
            opportunity_id=deal.pk if deal else None,
        )
