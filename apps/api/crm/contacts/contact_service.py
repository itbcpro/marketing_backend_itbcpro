"""Contact use cases: create, update, upsert, delete, restore, erase, ownership."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.common.normalize import InvalidValueError, normalise_email, normalise_phone
from crm.contacts import events, identities, tags
from crm.contacts.models import (
    Company,
    Contact,
    ContactFollower,
    ContactType,
    CrmSettings,
    IdentityChannel,
    MatchOrder,
)
from crm.events.publisher import publish
from crm.fields import services as fields
from crm.plans import services as plans
from workspaces.models import Membership

SCALAR_FIELDS = (
    "first_name",
    "last_name",
    "contact_type",
    "source",
    "time_zone",
    "date_of_birth",
    "address",
    "city",
    "state",
    "postal_code",
    "country",
    "attribution",
)
TEXT_LIMITS = {
    "first_name": 100,
    "last_name": 100,
    "source": 100,
    "address": 300,
    "city": 100,
    "state": 100,
    "postal_code": 20,
}
COUNTRY_LENGTH = 2
OBJECT_TYPE = "contact"


def get_settings(workspace_id: uuid.UUID) -> CrmSettings:
    row, _ = CrmSettings.objects.get_or_create(workspace_id=workspace_id)
    return row


def get_contact(
    workspace_id: uuid.UUID, contact_id: uuid.UUID, *, include_deleted: bool = False
) -> Contact:
    rows = Contact.objects.for_workspace(workspace_id).filter(pk=contact_id)
    found = (rows if include_deleted else rows.active()).first()
    if found is None:
        raise NotFoundError("contact not found")
    return found


# ---- input cleaning ---------------------------------------------------------------------


def _clean(workspace_id: uuid.UUID, data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise the keys present in ``data``; collect every problem at once."""
    errors: dict[str, str] = {}
    out: dict[str, Any] = {}

    def attempt(key: str, convert: Any) -> None:
        if key in data:
            try:
                out[key] = convert(data[key])
            except (InvalidValueError, ValueError) as exc:
                errors[key] = str(exc)

    attempt("email", lambda v: normalise_email(v) if v else "")
    attempt("phone", lambda v: normalise_phone(v) if v else "")
    attempt("extra_emails", lambda v: [normalise_email(x) for x in v])
    attempt("extra_phones", lambda v: [normalise_phone(x) for x in v])
    for key, limit in TEXT_LIMITS.items():
        attempt(key, lambda v, limit=limit: _text(v, limit))
    attempt("country", _country)
    attempt("time_zone", _zone)
    attempt("contact_type", _type)
    attempt("date_of_birth", lambda v: None if v in (None, "") else date.fromisoformat(str(v)))
    attempt("attribution", _mapping)
    if "company_id" in data:
        out["company_id"] = _company(workspace_id, data["company_id"], errors)
    if "assigned_to" in data:
        out["assigned_to"] = _member(workspace_id, data["assigned_to"], errors)
    if errors:
        raise ValidationFailedError("invalid contact", errors=errors)
    return out


def validate_contact_data(workspace_id: uuid.UUID, data: dict[str, Any]) -> dict[str, Any]:
    """Run the same validation as create and update without writing; raises with all problems."""
    return _clean(workspace_id, data)


def _text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > limit:
        raise InvalidValueError("too_long")
    return text


def _country(value: Any) -> str:
    text = _text(value, COUNTRY_LENGTH).upper()
    if text and (len(text) != COUNTRY_LENGTH or not text.isalpha()):
        raise InvalidValueError("invalid_country")
    return text


def _zone(value: Any) -> str:
    text = _text(value, 64)
    if text:
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidValueError("invalid_time_zone") from exc
    return text


def _type(value: Any) -> str:
    if value not in ContactType.values:
        raise InvalidValueError("invalid_type")
    return str(value)


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidValueError("invalid_attribution")
    return value


def _company(workspace_id: uuid.UUID, value: Any, errors: dict[str, str]) -> uuid.UUID | None:
    if value is None:
        return None
    exists = (
        Company.objects.for_workspace(workspace_id)
        .filter(pk=value, deleted_at__isnull=True)
        .exists()
    )
    if not exists:
        errors["company_id"] = "unknown_company"
    return value if exists else None


def _member(workspace_id: uuid.UUID, value: Any, errors: dict[str, str]) -> uuid.UUID | None:
    if value is None:
        return None
    if not Membership.objects.filter(workspace_id=workspace_id, user_id=value).exists():
        errors["assigned_to"] = "not_a_member"
        return None
    return value  # type: ignore[no-any-return]


# ---- create / update --------------------------------------------------------------------


def _identity_lists(
    contact: Contact | None, clean: dict[str, Any]
) -> tuple[str, list[str], str, list[str]]:
    """Resolve primary and extra email and phone after applying ``clean`` over the current state."""
    current = identities.addresses_of(contact) if contact else identities.Addresses()
    email = clean.get("email", current.email)
    phone = clean.get("phone", current.phone)
    extra_emails = [e for e in clean.get("extra_emails", current.extra_emails) if e != email]
    extra_phones = [p for p in clean.get("extra_phones", current.extra_phones) if p != phone]
    if (extra_emails and not email) or (extra_phones and not phone):
        raise ValidationFailedError(
            "extra address without primary",
            errors={"email" if extra_emails and not email else "phone": "primary_required"},
        )
    return email, list(dict.fromkeys(extra_emails)), phone, list(dict.fromkeys(extra_phones))


def create_contact(*, workspace_id: uuid.UUID, actor: Actor, data: dict[str, Any]) -> Contact:
    clean = _clean(workspace_id, data)
    email, extra_emails, phone, extra_phones = _identity_lists(None, clean)
    plans.ensure_room(
        workspace_id,
        plans.CONTACTS,
        lambda: Contact.objects.for_workspace(workspace_id).active().count(),
    )
    with transaction.atomic():
        identities.assert_free(workspace_id, None, email, extra_emails, phone, extra_phones)
        contact = Contact(workspace_id=workspace_id, email=email, phone=phone)
        for key in (*SCALAR_FIELDS, "company_id", "assigned_to"):
            if key in clean:
                setattr(contact, key if key != "assigned_to" else "assigned_to_id", clean[key])
        contact.save()
        identities.sync(contact, email, extra_emails, phone, extra_phones)
        _apply_extras(contact, actor, data)
        record_event(
            action="crm.contact.created",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_contact",
            target_id=contact.pk,
        )
        publish(
            "crm.contact.created",
            workspace_id=workspace_id,
            actor=actor,
            data=events.ContactCreated(contact_id=contact.pk, source=contact.source),
        )
        if contact.assigned_to_id:
            publish(
                "crm.contact.assigned",
                workspace_id=workspace_id,
                actor=actor,
                data=events.ContactAssigned(
                    contact_id=contact.pk, assigned_to=contact.assigned_to_id
                ),
            )
    return contact


def _apply_extras(contact: Contact, actor: Actor, data: dict[str, Any]) -> None:
    if data.get("custom"):
        fields.set_values(
            workspace_id=contact.workspace_id,
            object_type=OBJECT_TYPE,
            record_id=contact.pk,
            values=data["custom"],
        )
    if data.get("tags"):
        tags.add_tags(
            workspace_id=contact.workspace_id, actor=actor, contact=contact, names=data["tags"]
        )


def update_contact(
    *, workspace_id: uuid.UUID, actor: Actor, contact_id: uuid.UUID, data: dict[str, Any]
) -> Contact:
    clean = _clean(workspace_id, data)
    with transaction.atomic():
        contact = (
            Contact.objects.select_for_update()
            .filter(workspace_id=workspace_id, pk=contact_id, deleted_at__isnull=True)
            .first()
        )
        if contact is None:
            raise NotFoundError("contact not found")
        return _update_locked(contact, actor, data, clean)


def _update_locked(
    contact: Contact, actor: Actor, data: dict[str, Any], clean: dict[str, Any]
) -> Contact:
    email, extra_emails, phone, extra_phones = _identity_lists(contact, clean)
    identities.assert_free(contact.workspace_id, contact, email, extra_emails, phone, extra_phones)
    before_owner = contact.assigned_to_id
    changed: list[str] = []
    for key in (*SCALAR_FIELDS, "company_id", "assigned_to"):
        if key not in clean:
            continue
        attr = "assigned_to_id" if key == "assigned_to" else key
        if getattr(contact, attr) != clean[key]:
            setattr(contact, attr, clean[key])
            changed.append(key)
    if (email, phone) != (contact.email, contact.phone):
        changed += [
            k
            for k, new, old in (("email", email, contact.email), ("phone", phone, contact.phone))
            if new != old
        ]
        contact.email, contact.phone = email, phone
    if identities.would_change(contact, email, extra_emails, phone, extra_phones):
        changed.append("addresses")
    contact.save()
    identities.sync(contact, email, extra_emails, phone, extra_phones)
    if data.get("custom"):
        changed.append("custom")
    _apply_extras(contact, actor, data)
    if changed or data.get("tags"):
        _announce_update(contact, actor, sorted(set(changed)), before_owner)
    return contact


def _announce_update(contact: Contact, actor: Actor, changed: list[str], before_owner: Any) -> None:
    record_event(
        action="crm.contact.updated",
        actor=actor,
        workspace_id=contact.workspace_id,
        target_type="crm_contact",
        target_id=contact.pk,
        after={"changed": changed},
    )
    publish(
        "crm.contact.updated",
        workspace_id=contact.workspace_id,
        actor=actor,
        data=events.ContactUpdated(contact_id=contact.pk, changed=changed),
    )
    if contact.assigned_to_id != before_owner:
        publish(
            "crm.contact.assigned",
            workspace_id=contact.workspace_id,
            actor=actor,
            data=events.ContactAssigned(contact_id=contact.pk, assigned_to=contact.assigned_to_id),
        )


def upsert_contact(
    *, workspace_id: uuid.UUID, actor: Actor, data: dict[str, Any]
) -> tuple[Contact, bool]:
    """Update the contact matching the email or phone (workspace order) or create one."""
    clean = _clean(workspace_id, data)
    with transaction.atomic():
        match = _match(workspace_id, clean)
        if match is None:
            return create_contact(workspace_id=workspace_id, actor=actor, data=data), True
        locked = Contact.objects.select_for_update().get(pk=match.pk)
        return _update_locked(locked, actor, data, clean), False


def _match(workspace_id: uuid.UUID, clean: dict[str, Any]) -> Contact | None:
    order = get_settings(workspace_id).match_order
    probes = [
        (IdentityChannel.EMAIL, clean.get("email")),
        (IdentityChannel.PHONE, clean.get("phone")),
    ]
    if order == MatchOrder.PHONE_FIRST:
        probes.reverse()
    found: list[Contact] = []
    for channel, value in probes:
        if value:
            hit = identities.find_contact(workspace_id, channel, value)
            if hit is not None:
                found.append(hit)
    if len({c.pk for c in found}) > 1:
        raise ConflictError(
            "email and phone belong to different contacts",
            errors={"email": "belongs_to_other_contact", "phone": "belongs_to_other_contact"},
        )
    return found[0] if found else None


# ---- delete / restore / erase -------------------------------------------------------------


def delete_contact(*, workspace_id: uuid.UUID, actor: Actor, contact_id: uuid.UUID) -> None:
    with transaction.atomic():
        contact = get_contact(workspace_id, contact_id)
        contact.deleted_at = timezone.now()
        contact.save(update_fields=["deleted_at", "updated_at"])
        identities.set_active(contact, active=False)
        record_event(
            action="crm.contact.deleted",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_contact",
            target_id=contact.pk,
        )
        publish(
            "crm.contact.deleted",
            workspace_id=workspace_id,
            actor=actor,
            data=events.ContactDeleted(contact_id=contact.pk),
        )


def restore_contact(*, workspace_id: uuid.UUID, actor: Actor, contact_id: uuid.UUID) -> Contact:
    with transaction.atomic():
        contact = get_contact(workspace_id, contact_id, include_deleted=True)
        if contact.deleted_at is None or contact.merged_into_id is not None:
            raise NotFoundError("contact not found")
        if timezone.now() - contact.deleted_at > timedelta(days=settings.CRM_RESTORE_DAYS):
            raise ConflictError("restore window has passed", errors={"contact": "restore_expired"})
        addresses = identities.addresses_of(contact, include_inactive=True)
        identities.assert_free(
            workspace_id,
            contact,
            addresses.email,
            addresses.extra_emails,
            addresses.phone,
            addresses.extra_phones,
        )
        contact.deleted_at = None
        contact.save(update_fields=["deleted_at", "updated_at"])
        identities.set_active(contact, active=True)
        record_event(
            action="crm.contact.restored",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_contact",
            target_id=contact.pk,
        )
        publish(
            "crm.contact.restored",
            workspace_id=workspace_id,
            actor=actor,
            data=events.ContactRestored(contact_id=contact.pk),
        )
    return contact


def erase_contact(*, workspace_id: uuid.UUID, actor: Actor, contact_id: uuid.UUID) -> None:
    """Privacy erasure: remove the contact and everything hanging off it (cascade and values)."""
    with transaction.atomic():
        contact = get_contact(workspace_id, contact_id, include_deleted=True)
        fields.delete_values_of(workspace_id, OBJECT_TYPE, contact.pk)
        contact_pk = contact.pk
        contact.delete()
        record_event(
            action="crm.contact.erased",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_contact",
            target_id=contact_pk,
        )
        publish(
            "crm.contact.erased",
            workspace_id=workspace_id,
            actor=actor,
            data=events.ContactErased(contact_id=contact_pk),
        )


# ---- ownership ---------------------------------------------------------------------------


def assign(
    *, workspace_id: uuid.UUID, actor: Actor, contact_id: uuid.UUID, user_id: uuid.UUID | None
) -> Contact:
    return update_contact(
        workspace_id=workspace_id, actor=actor, contact_id=contact_id, data={"assigned_to": user_id}
    )


def assign_round_robin(
    *, workspace_id: uuid.UUID, actor: Actor, contact_id: uuid.UUID, user_ids: list[uuid.UUID]
) -> Contact:
    """Give the contact to the next user after the saved cursor, cycling through ``user_ids``."""
    if not user_ids:
        raise ValidationFailedError("no candidates", errors={"user_ids": "required"})
    ordered = sorted(set(user_ids))
    with transaction.atomic():
        state = CrmSettings.objects.select_for_update().get_or_create(workspace_id=workspace_id)[0]
        after = [
            u for u in ordered if state.round_robin_cursor is None or u > state.round_robin_cursor
        ]
        chosen = (after or ordered)[0]
        state.round_robin_cursor = chosen
        state.save(update_fields=["round_robin_cursor", "updated_at"])
        return assign(workspace_id=workspace_id, actor=actor, contact_id=contact_id, user_id=chosen)


def add_follower(*, workspace_id: uuid.UUID, contact_id: uuid.UUID, user_id: uuid.UUID) -> None:
    contact = get_contact(workspace_id, contact_id)
    if not Membership.objects.filter(workspace_id=workspace_id, user_id=user_id).exists():
        raise ValidationFailedError("not a member", errors={"user_id": "not_a_member"})
    ContactFollower.objects.get_or_create(
        workspace_id=workspace_id, contact=contact, user_id=user_id
    )


def remove_follower(*, workspace_id: uuid.UUID, contact_id: uuid.UUID, user_id: uuid.UUID) -> None:
    ContactFollower.objects.for_workspace(workspace_id).filter(
        contact_id=contact_id, user_id=user_id
    ).delete()
