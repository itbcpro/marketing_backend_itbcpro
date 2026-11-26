"""Use cases of custom fields, values and workspace constants (public surface of the module)."""

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.common.normalize import InvalidValueError
from crm.events.publisher import publish
from crm.fields import events
from crm.fields.models import (
    KEY_PATTERN,
    FieldDefinition,
    FieldValue,
    ObjectType,
    WorkspaceValue,
)
from crm.fields.reserved import RESERVED_KEYS
from crm.fields.types import HANDLERS, OPTION_TYPES, Stored

MAX_OPTIONS = 200
_KEY = re.compile(KEY_PATTERN)


def _check_key(object_type: str, key: str) -> None:
    if not _KEY.match(key):
        raise ValidationFailedError("invalid key", field="key", reason="invalid_key")
    if key in RESERVED_KEYS.get(object_type, frozenset()):
        raise ValidationFailedError("reserved key", field="key", reason="reserved_key")


def _clean_options(field_type: str, options: list[dict[str, str]]) -> list[dict[str, str]]:
    if field_type not in OPTION_TYPES:
        return []
    invalid = ValidationFailedError("invalid options", field="options", reason="invalid_options")
    if not options or len(options) > MAX_OPTIONS:
        raise invalid
    cleaned = [
        {"value": str(o["value"]).strip(), "label": str(o.get("label") or o["value"])}
        for o in options
    ]
    values = [o["value"] for o in cleaned]
    if len(set(values)) != len(values) or not all(values):
        raise invalid
    return cleaned


def create_definition(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    object_type: str,
    key: str,
    label: str,
    field_type: str,
    options: list[dict[str, str]] | None = None,
    folder: str = "",
) -> FieldDefinition:
    if object_type not in ObjectType.values or field_type not in HANDLERS:
        raise ValidationFailedError("invalid type", field="field_type", reason="invalid_type")
    _check_key(object_type, key)
    cleaned = _clean_options(field_type, options or [])
    with transaction.atomic():
        scoped = FieldDefinition.objects.filter(workspace_id=workspace_id, object_type=object_type)
        if scoped.filter(key=key).exists():
            raise ConflictError("field key already exists")
        definition = FieldDefinition.objects.create(
            workspace_id=workspace_id,
            object_type=object_type,
            key=key,
            label=label.strip(),
            field_type=field_type,
            options=cleaned,
            folder=folder.strip(),
            position=scoped.count(),
        )
        record_event(
            action="crm.field.created",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_field",
            target_id=definition.pk,
            after={"key": key, "field_type": field_type},
        )
        publish(
            "crm.field.created",
            workspace_id=workspace_id,
            actor=actor,
            data=events.FieldCreated(
                field_id=definition.pk, object_type=object_type, key=key, field_type=field_type
            ),
        )
    return definition


def get_definition(workspace_id: uuid.UUID, field_id: uuid.UUID) -> FieldDefinition:
    found = FieldDefinition.objects.for_workspace(workspace_id).filter(pk=field_id).first()
    if found is None:
        raise NotFoundError("field not found")
    return found


def update_definition(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    field_id: uuid.UUID,
    label: str | None = None,
    options: list[dict[str, str]] | None = None,
    folder: str | None = None,
) -> FieldDefinition:
    """Key and type are immutable. Options may be relabelled; stored values stay readable."""
    with transaction.atomic():
        definition = get_definition(workspace_id, field_id)
        if label is not None:
            definition.label = label.strip()
        if folder is not None:
            definition.folder = folder.strip()
        if options is not None:
            definition.options = _clean_options(definition.field_type, options)
        definition.save()
        record_event(
            action="crm.field.updated",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_field",
            target_id=definition.pk,
        )
    return definition


def reorder(*, workspace_id: uuid.UUID, object_type: str, ordered_ids: list[uuid.UUID]) -> None:
    with transaction.atomic():
        rows = {
            d.pk: d
            for d in FieldDefinition.objects.select_for_update().filter(
                workspace_id=workspace_id, object_type=object_type
            )
        }
        if set(ordered_ids) != set(rows) or len(ordered_ids) != len(rows):
            raise ValidationFailedError("ids must list every field", reason="invalid_order")
        for position, field_id in enumerate(ordered_ids):
            if rows[field_id].position != position:
                rows[field_id].position = position
                rows[field_id].save(update_fields=["position", "updated_at"])


MAX_FOLDER = 100


def rename_folder(
    *, workspace_id: uuid.UUID, actor: Actor, object_type: str, old: str, new: str
) -> int:
    """Rename a folder of one object type; an empty ``new`` moves its fields out of any folder."""
    if object_type not in ObjectType.values:
        raise ValidationFailedError("invalid object type", field="object_type", reason="invalid")
    target = new.strip()
    if len(target) > MAX_FOLDER:
        raise ValidationFailedError("invalid folder", field="new", reason="too_long")
    with transaction.atomic():
        rows = FieldDefinition.objects.for_workspace(workspace_id).filter(
            object_type=object_type, folder=old.strip()
        )
        changed = rows.update(folder=target, updated_at=timezone.now())
        if changed:
            record_event(
                action="crm.field.folder_renamed",
                actor=actor,
                workspace_id=workspace_id,
                target_type="crm_field_folder",
                target_id=workspace_id,
                after={"object_type": object_type, "old": old, "new": target},
            )
    return changed


def archive_definition(*, workspace_id: uuid.UUID, actor: Actor, field_id: uuid.UUID) -> None:
    with transaction.atomic():
        definition = get_definition(workspace_id, field_id)
        if definition.archived_at is not None:
            return
        definition.archived_at = timezone.now()
        definition.save(update_fields=["archived_at", "updated_at"])
        record_event(
            action="crm.field.archived",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_field",
            target_id=definition.pk,
        )
        publish(
            "crm.field.archived",
            workspace_id=workspace_id,
            actor=actor,
            data=events.FieldArchived(
                field_id=definition.pk, object_type=definition.object_type, key=definition.key
            ),
        )


def list_definitions(
    workspace_id: uuid.UUID, object_type: str, *, include_archived: bool = False
) -> list[FieldDefinition]:
    rows = FieldDefinition.objects.for_workspace(workspace_id).filter(object_type=object_type)
    return list(rows if include_archived else rows.filter(archived_at__isnull=True))


def _prepare(
    definitions: dict[str, FieldDefinition], values: dict[str, Any]
) -> list[tuple[FieldDefinition, Stored | None]]:
    errors: dict[str, str] = {}
    prepared: list[tuple[FieldDefinition, Stored | None]] = []
    for key, raw in values.items():
        definition = definitions.get(key)
        if definition is None:
            errors[key] = "unknown_field"
        elif definition.archived_at is not None:
            errors[key] = "field_archived"
        elif raw is None:
            prepared.append((definition, None))
        else:
            try:
                handler = HANDLERS[definition.field_type]
                prepared.append((definition, handler.normalise(raw, definition.options)))
            except InvalidValueError as exc:
                errors[key] = str(exc)
    if errors:
        raise ValidationFailedError("invalid custom values", errors=errors)
    return prepared


def set_values(
    *,
    workspace_id: uuid.UUID,
    object_type: str,
    record_id: uuid.UUID,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Validate and store custom values; ``None`` clears one. Returns all values of the record."""
    if values:
        definitions = {
            d.key: d
            for d in FieldDefinition.objects.for_workspace(workspace_id).filter(
                object_type=object_type, key__in=list(values)
            )
        }
        prepared = _prepare(definitions, values)
        with transaction.atomic():
            for definition, stored in prepared:
                if stored is None:
                    FieldValue.objects.filter(field=definition, record_id=record_id).delete()
                    continue
                FieldValue.objects.update_or_create(
                    field=definition,
                    record_id=record_id,
                    defaults={
                        "workspace_id": workspace_id,
                        "object_type": object_type,
                        "value_text": stored.text,
                        "value_number": stored.number,
                        "value_date": stored.day,
                        "value_json": stored.json,
                    },
                )
    return get_values(workspace_id, object_type, [record_id]).get(record_id, {})


def get_values(
    workspace_id: uuid.UUID, object_type: str, record_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Values by record and key. Archived fields stay readable."""
    out: dict[uuid.UUID, dict[str, Any]] = defaultdict(dict)
    rows = (
        FieldValue.objects.for_workspace(workspace_id)
        .filter(object_type=object_type, record_id__in=list(record_ids))
        .select_related("field")
    )
    for row in rows:
        out[row.record_id][row.field.key] = HANDLERS[row.field.field_type].read(row)
    return dict(out)


def delete_values_of(workspace_id: uuid.UUID, object_type: str, record_id: uuid.UUID) -> None:
    FieldValue.objects.for_workspace(workspace_id).filter(
        object_type=object_type, record_id=record_id
    ).delete()


def move_missing_values(
    workspace_id: uuid.UUID, object_type: str, *, from_id: uuid.UUID, to_id: uuid.UUID
) -> list[str]:
    """Give ``to_id`` the values ``from_id`` has and ``to_id`` lacks; return the moved field ids."""
    scoped = FieldValue.objects.for_workspace(workspace_id).filter(object_type=object_type)
    have = set(scoped.filter(record_id=to_id).values_list("field_id", flat=True))
    moving = [row for row in scoped.filter(record_id=from_id) if row.field_id not in have]
    for row in moving:
        row.record_id = to_id
        row.save(update_fields=["record_id", "updated_at"])
    return [str(row.field_id) for row in moving]


def restore_values(
    workspace_id: uuid.UUID,
    object_type: str,
    *,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    field_ids: list[str],
) -> None:
    """Undo ``move_missing_values``: send the listed fields back from ``from_id`` to ``to_id``."""
    FieldValue.objects.for_workspace(workspace_id).filter(
        object_type=object_type, record_id=from_id, field_id__in=[uuid.UUID(f) for f in field_ids]
    ).update(record_id=to_id)


# ---- querying by custom value ---------------------------------------------------------------

TEXT_TYPES = frozenset({"text", "long_text", "phone", "email", "url", "select"})
OPERATORS_BY_TYPE: dict[str, frozenset[str]] = {
    **{t: frozenset({"eq", "neq", "contains", "in", "empty", "not_empty"}) for t in TEXT_TYPES},
    "number": frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "empty", "not_empty"}),
    "money": frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "empty", "not_empty"}),
    "date": frozenset({"eq", "before", "after", "empty", "not_empty"}),
    "multi_select": frozenset({"has", "empty", "not_empty"}),
    "checkbox": frozenset({"is_true", "is_false"}),
}
_NUMBER_LOOKUPS = {"eq": "", "neq": "", "gt": "__gt", "gte": "__gte", "lt": "__lt", "lte": "__lte"}


def matching_record_ids(
    workspace_id: uuid.UUID, object_type: str, key: str, operator: str, value: Any
) -> tuple[Any, bool]:
    """Record ids whose custom value satisfies a condition.

    Returns ``(ids, negate)``: when ``negate`` is true the condition holds for the records that
    are NOT in ``ids`` (for example "is empty" or "not equal").
    """
    definition = (
        FieldDefinition.objects.for_workspace(workspace_id)
        .filter(object_type=object_type, key=key)
        .first()
    )
    if definition is None:
        raise ValidationFailedError("unknown field", errors={f"custom.{key}": "unknown_field"})
    if operator not in OPERATORS_BY_TYPE[definition.field_type]:
        raise ValidationFailedError(
            "invalid operator", errors={f"custom.{key}": "invalid_operator"}
        )
    rows = FieldValue.objects.for_workspace(workspace_id).filter(field=definition)
    field_type = definition.field_type
    try:
        if operator in {"empty", "not_empty"}:
            return rows.values("record_id"), operator == "empty"
        if field_type in TEXT_TYPES:
            lookup = {
                "eq": ("value_text__iexact", value),
                "neq": ("value_text__iexact", value),
                "contains": ("value_text__icontains", value),
                "in": ("value_text__in", list(value)),
            }[operator]
        elif field_type in {"number", "money"}:
            lookup = (f"value_number{_NUMBER_LOOKUPS[operator]}", Decimal(str(value)))
        elif field_type == "date":
            lookups = {"eq": "value_date", "before": "value_date__lt", "after": "value_date__gt"}
            lookup = (lookups[operator], date.fromisoformat(str(value)))
        elif field_type == "multi_select":
            lookup = ("value_json__contains", [str(value)])
        else:
            return rows.filter(value_json=True).values("record_id"), operator == "is_false"
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValidationFailedError(
            "invalid value", errors={f"custom.{key}": "invalid_value"}
        ) from exc
    return rows.filter(**{lookup[0]: lookup[1]}).values("record_id"), operator == "neq"


# ---- workspace values ---------------------------------------------------------------------


def set_workspace_value(
    *, workspace_id: uuid.UUID, key: str, label: str, value: str
) -> WorkspaceValue:
    if not _KEY.match(key):
        raise ValidationFailedError("invalid key", field="key", reason="invalid_key")
    row, _ = WorkspaceValue.objects.update_or_create(
        workspace_id=workspace_id, key=key, defaults={"label": label.strip(), "value": value}
    )
    return row


def delete_workspace_value(*, workspace_id: uuid.UUID, key: str) -> None:
    deleted, _ = WorkspaceValue.objects.for_workspace(workspace_id).filter(key=key).delete()
    if not deleted:
        raise NotFoundError("workspace value not found")


def workspace_values(workspace_id: uuid.UUID) -> dict[str, str]:
    return dict(WorkspaceValue.objects.for_workspace(workspace_id).values_list("key", "value"))
