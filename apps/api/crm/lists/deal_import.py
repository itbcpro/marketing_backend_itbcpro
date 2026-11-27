"""Deal (opportunity) rows of a CSV import: validate a row, then write it.

Each row names a contact (by email, phone or name), a pipeline and a stage. Unknown pipelines,
stages, lost reasons and contacts are created when ``create_missing`` is on, so a file exported from
another CRM can be loaded without any set-up. A deal that already exists (same contact, pipeline and
name) is left alone, which makes a second run harmless.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit.actors import Actor
from core.errors import ValidationFailedError
from crm.activity import services as activity
from crm.contacts import services as contacts
from crm.fields import services as fields
from crm.pipelines import services as pipelines
from workspaces.models import Membership

CUSTOM_PREFIX = "custom."
MICROS = 1_000_000
STANDARD_TARGETS = (
    "name",
    "contact_name",
    "contact_email",
    "contact_phone",
    "pipeline",
    "stage",
    "value",
    "source",
    "owner",
    "expected_close_date",
    "status",
    "lost_reason",
    "notes",
    "created_at",
    "tags",
)
ALIASES = {
    "opportunityname": "name",
    "dealname": "name",
    "email": "contact_email",
    "phone": "contact_phone",
    "leadvalue": "value",
    "amount": "value",
    "assigned": "owner",
    "assignedto": "owner",
    "createdon": "created_at",
    "lostreasonname": "lost_reason",
    "expectedclosedate": "expected_close_date",
    "tag": "tags",
}
OBJECT_TYPE = "opportunity"


def custom_targets(workspace_id: uuid.UUID) -> set[str]:
    return {f"{CUSTOM_PREFIX}{d.key}" for d in fields.list_definitions(workspace_id, OBJECT_TYPE)}


def all_targets(workspace_id: uuid.UUID) -> set[str]:
    return {*STANDARD_TARGETS, *custom_targets(workspace_id)}


def check_mapping(used: list[str]) -> dict[str, str]:
    """Problems with the chosen columns: a deal needs a pipeline and someone to belong to."""
    errors: dict[str, str] = {}
    if "pipeline" not in used:
        errors["mapping"] = "pipeline_required"
    elif not {"contact_email", "contact_phone", "contact_name"} & set(used):
        errors["mapping"] = "contact_required"
    elif not {"name", "contact_name"} & set(used):
        errors["mapping"] = "name_required"
    return errors


def _fail(**errors: str) -> ValidationFailedError:
    return ValidationFailedError("invalid deal", errors=errors)


def _value_micros(text: str) -> int:
    try:
        amount = Decimal(text.replace(",", "").lstrip("$"))
    except InvalidOperation as exc:
        raise _fail(value="invalid_number") from exc
    if amount < 0:
        raise _fail(value="invalid_number")
    return int(amount * MICROS)


def _when(text: str) -> datetime:
    parsed = parse_datetime(text)
    if parsed is None:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError as exc:
            raise _fail(created_at="invalid_date") from exc
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _errors_of(exc: ValidationFailedError) -> dict[str, str]:
    return cast("dict[str, str]", exc.params.get("errors", {}))


def _numbers_and_dates(data: dict[str, Any], out: dict[str, Any]) -> dict[str, str]:
    """Convert the value and date cells into ``out``; returns the problems found."""
    errors: dict[str, str] = {}
    for key, convert in (
        ("value", _value_micros),
        ("created_at", _when),
        ("expected_close_date", lambda t: date.fromisoformat(t[:10])),
    ):
        try:
            out[key] = convert(data[key]) if data.get(key) else None
        except ValidationFailedError as exc:
            errors.update(_errors_of(exc))
        except ValueError:
            errors[key] = "invalid_date"
    return errors


def parse(workspace_id: uuid.UUID, data: dict[str, Any]) -> dict[str, Any]:
    """Validate one row without writing; returns the cleaned values."""
    errors: dict[str, str] = {}
    out: dict[str, Any] = {"contact_name": data.get("contact_name", "")}
    out["name"] = data.get("name") or out["contact_name"]
    if not out["name"]:
        errors["name"] = "required"
    out["pipeline"] = data.get("pipeline", "")
    if not out["pipeline"]:
        errors["pipeline"] = "required"
    out["stage"] = data.get("stage", "")
    identity = {
        key: data[f"contact_{key}"] for key in ("email", "phone") if data.get(f"contact_{key}")
    }
    try:
        out["identity"] = contacts.validate_contact_data(workspace_id, identity)
    except ValidationFailedError as exc:
        errors.update({f"contact_{k}": v for k, v in _errors_of(exc).items()})
    if not identity and not out["contact_name"]:
        errors["contact_name"] = "required"
    status = data.get("status", "open").lower()
    if status not in pipelines.STATUSES:
        errors["status"] = "invalid_value"
    out["status"] = status
    errors.update(_numbers_and_dates(data, out))
    for key in ("source", "owner", "lost_reason", "notes"):
        out[key] = data.get(key, "")
    out["tags"] = data.get("tags", [])
    out["custom"] = data.get("custom", {})
    if errors:
        raise ValidationFailedError("invalid deal", errors=errors)
    return out


def _owner(workspace_id: uuid.UUID, text: str) -> uuid.UUID | None:
    """The member whose name or email is ``text``; nobody if there is no such member."""
    wanted = " ".join(text.split()).casefold()
    if not wanted:
        return None
    for member in Membership.objects.filter(workspace_id=workspace_id).select_related("user"):
        names = {member.user.email.casefold(), " ".join(member.user.full_name.split()).casefold()}
        if wanted in names:
            return member.user_id
    return None


def _contact(
    workspace_id: uuid.UUID, actor: Actor, row: dict[str, Any], *, create_missing: bool
) -> Any:
    for channel in ("email", "phone"):
        value = row["identity"].get(channel)
        found = contacts.find_contact(workspace_id, channel, value) if value else None
        if found is not None:
            return found
    if not create_missing:
        raise _fail(contact_email="unknown_contact")
    first, _, last = row["contact_name"].partition(" ")
    return contacts.create_contact(
        workspace_id=workspace_id,
        actor=actor,
        data={"first_name": first, "last_name": last.strip(), **row["identity"]},
    )


def write(
    *, workspace_id: uuid.UUID, actor: Actor, data: dict[str, Any], options: dict[str, Any]
) -> bool:
    """Create the deal of one row. True when created, False when it already existed."""
    row = parse(workspace_id, data)
    create_missing = bool(options.get("create_missing", True))
    stage = pipelines.resolve_stage(
        workspace_id, pipeline=row["pipeline"], stage=row["stage"], create_missing=create_missing
    )
    contact = _contact(workspace_id, actor, row, create_missing=create_missing)
    if pipelines.find_deal(
        workspace_id, contact_id=contact.pk, pipeline_id=stage.pipeline_id, name=row["name"]
    ):
        return False
    deal = pipelines.create_opportunity(
        workspace_id,
        actor,
        contact_id=contact.pk,
        pipeline_id=stage.pipeline_id,
        stage_id=stage.pk,
        name=row["name"],
        value_micros=row["value"] or 0,
        currency=settings.CRM_WALLET_CURRENCY,
        assigned_to=_owner(workspace_id, row["owner"]),
        source=row["source"],
        expected_close_date=row["expected_close_date"],
        tags=[*options.get("tags", []), *row["tags"]],
        custom=row["custom"],
        backdate_to=row["created_at"],
    )
    _close(workspace_id, actor, deal, row, create_missing=create_missing)
    if row["notes"].strip():
        activity.add_note(
            workspace_id=workspace_id,
            actor=actor,
            contact_id=contact.pk,
            body=row["notes"],
            opportunity_id=deal.pk,
        )
    return True


def _close(
    workspace_id: uuid.UUID, actor: Actor, deal: Any, row: dict[str, Any], *, create_missing: bool
) -> None:
    if row["status"] == "won":
        pipelines.win(workspace_id, actor, deal)
    elif row["status"] == "abandoned":
        pipelines.abandon(workspace_id, actor, deal)
    elif row["status"] == "lost":
        reason = (
            pipelines.resolve_lost_reason(
                workspace_id, row["lost_reason"], create_missing=create_missing
            )
            if row["lost_reason"]
            else None
        )
        pipelines.lose(workspace_id, actor, deal, lost_reason_id=reason)
