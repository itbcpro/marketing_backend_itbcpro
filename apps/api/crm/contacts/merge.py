"""Merging two contacts, with an undo window."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.contacts import events, identities
from crm.contacts.contact_service import OBJECT_TYPE, SCALAR_FIELDS, get_contact
from crm.contacts.models import Contact, ContactFollower, ContactIdentity, ContactMerge, ContactTag
from crm.events.publisher import publish
from crm.fields import services as fields

MERGEABLE = (*SCALAR_FIELDS, "company_id", "assigned_to_id")
WINNER, LOSER = "winner", "loser"


def _value(contact: Contact, name: str) -> Any:
    value = getattr(contact, name)
    return (
        value.isoformat()
        if hasattr(value, "isoformat")
        else (str(value) if isinstance(value, uuid.UUID) else value)
    )


def _snapshot(contact: Contact) -> dict[str, Any]:
    return {name: _value(contact, name) for name in MERGEABLE}


def _is_empty(value: Any) -> bool:
    return value in ("", None, {}, [])


def merge_contacts(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    winner_id: uuid.UUID,
    loser_id: uuid.UUID,
    choices: dict[str, str] | None = None,
) -> ContactMerge:
    """Fold ``loser`` into ``winner``. ``choices`` picks the value per field; default is winner,
    falling back to the loser's value when the winner has none."""
    if winner_id == loser_id:
        raise ValidationFailedError("same contact", errors={"loser_id": "same_contact"})
    choices = choices or {}
    unknown = set(choices) - set(MERGEABLE) - {"company", "assigned_to"}
    if unknown or not set(choices.values()) <= {WINNER, LOSER}:
        raise ValidationFailedError("invalid choices", errors={"choices": "invalid_choices"})
    with transaction.atomic():
        first, second = sorted([winner_id, loser_id])
        locked = {
            c.pk: c
            for c in Contact.objects.select_for_update()
            .filter(workspace_id=workspace_id, pk__in=[first, second], deleted_at__isnull=True)
            .order_by("pk")
        }
        if len(locked) != 2:  # noqa: PLR2004 - exactly the two requested contacts
            raise NotFoundError("contact not found")
        winner, loser = locked[winner_id], locked[loser_id]
        snapshot: dict[str, Any] = {
            "winner_before": _snapshot(winner),
            "winner_email": winner.email,
            "winner_phone": winner.phone,
            "loser": _snapshot(loser),
            "loser_email": loser.email,
            "loser_phone": loser.phone,
        }
        _merge_scalars(winner, loser, choices)
        snapshot["moved_identities"] = _move_identities(winner, loser)
        snapshot["added_tags"] = _move_links(ContactTag, "tag_id", winner, loser)
        snapshot["added_followers"] = _move_links(ContactFollower, "user_id", winner, loser)
        snapshot["moved_values"] = fields.move_missing_values(
            workspace_id, OBJECT_TYPE, from_id=loser.pk, to_id=winner.pk
        )
        loser.merged_into = winner
        loser.deleted_at = timezone.now()
        loser.email, loser.phone = "", ""
        loser.save()
        winner.save()
        merge = ContactMerge.objects.create(
            workspace_id=workspace_id,
            winner=winner,
            loser=loser,
            snapshot=snapshot,
            merged_by_id=uuid.UUID(actor.id) if actor.type == "human" else None,
        )
        record_event(
            action="crm.contact.merged",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_contact",
            target_id=winner.pk,
            after={"loser_id": str(loser.pk)},
        )
        publish(
            "crm.contact.merged",
            workspace_id=workspace_id,
            actor=actor,
            data=events.ContactMerged(merge_id=merge.pk, winner_id=winner.pk, loser_id=loser.pk),
        )
    return merge


def _merge_scalars(winner: Contact, loser: Contact, choices: dict[str, str]) -> None:
    for name in MERGEABLE:
        public = "assigned_to" if name == "assigned_to_id" else name.removesuffix("_id")
        pick = choices.get(name) or choices.get(public)
        mine, theirs = getattr(winner, name), getattr(loser, name)
        take_loser = pick == LOSER or (pick is None and _is_empty(mine) and not _is_empty(theirs))
        if take_loser:
            setattr(winner, name, theirs)


def _move_identities(winner: Contact, loser: Contact) -> list[str]:
    """Move the loser's identities; the winner keeps its own primary email and phone."""
    moved: list[str] = []
    for row in ContactIdentity.objects.filter(contact=loser, is_active=True):
        primary_taken = (
            row.is_primary
            and ContactIdentity.objects.filter(
                contact=winner, channel=row.channel, is_primary=True, is_active=True
            ).exists()
        )
        row.contact = winner
        row.is_primary = row.is_primary and not primary_taken
        row.save(update_fields=["contact", "is_primary", "updated_at"])
        moved.append(str(row.pk))
    addresses = identities.addresses_of(winner)
    winner.email, winner.phone = addresses.email, addresses.phone
    return moved


def _move_links(model: Any, key: str, winner: Contact, loser: Contact) -> list[str]:
    """Give the winner every link the loser has and the winner lacks; return the created ids."""
    have = set(model.objects.filter(contact=winner).values_list(key, flat=True))
    missing = [
        getattr(link, key)
        for link in model.objects.filter(contact=loser)
        if getattr(link, key) not in have
    ]
    return [
        str(
            model.objects.create(
                workspace_id=winner.workspace_id, contact=winner, **{key: value}
            ).pk
        )
        for value in missing
    ]


def undo_merge(*, workspace_id: uuid.UUID, actor: Actor, merge_id: uuid.UUID) -> ContactMerge:
    window = timedelta(hours=settings.CRM_MERGE_UNDO_HOURS)
    with transaction.atomic():
        merge = (
            ContactMerge.objects.select_for_update()
            .select_related("winner", "loser")
            .filter(workspace_id=workspace_id, pk=merge_id)
            .first()
        )
        if merge is None:
            raise NotFoundError("merge not found")
        if merge.undone_at is not None or timezone.now() - merge.created_at > window:
            raise ConflictError("merge cannot be undone", errors={"merge": "undo_expired"})
        winner = Contact.objects.select_for_update().get(pk=merge.winner_id)
        loser = Contact.objects.select_for_update().get(pk=merge.loser_id)
        if winner.deleted_at is not None:
            raise ConflictError("winner no longer active", errors={"merge": "winner_inactive"})
        snap = merge.snapshot
        for name, old in snap["winner_before"].items():
            _restore(winner, name, old)
        winner.email, winner.phone = snap["winner_email"], snap["winner_phone"]
        ContactIdentity.objects.filter(pk__in=snap["moved_identities"]).update(contact=loser)
        for row in ContactIdentity.objects.filter(pk__in=snap["moved_identities"]):
            row.is_primary = row.value in (snap["loser_email"], snap["loser_phone"])
            row.save(update_fields=["is_primary", "updated_at"])
        ContactTag.objects.filter(pk__in=snap["added_tags"]).delete()
        ContactFollower.objects.filter(pk__in=snap["added_followers"]).delete()
        fields.restore_values(
            workspace_id,
            OBJECT_TYPE,
            from_id=winner.pk,
            to_id=loser.pk,
            field_ids=snap["moved_values"],
        )
        loser.merged_into, loser.deleted_at = None, None
        loser.email, loser.phone = snap["loser_email"], snap["loser_phone"]
        winner.save()  # release the addresses first, then hand them back
        loser.save()
        merge.undone_at = timezone.now()
        merge.save(update_fields=["undone_at", "updated_at"])
        record_event(
            action="crm.contact.merge_undone",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_contact",
            target_id=winner.pk,
        )
        publish(
            "crm.contact.merge_undone",
            workspace_id=workspace_id,
            actor=actor,
            data=events.ContactMergeUndone(
                merge_id=merge.pk, winner_id=winner.pk, loser_id=loser.pk
            ),
        )
    return merge


def _restore(contact: Contact, name: str, old: Any) -> None:
    field = Contact._meta.get_field(name.removesuffix("_id") if name.endswith("_id") else name)
    if old is not None and field.get_internal_type() == "DateField":
        old = date.fromisoformat(old)
    setattr(contact, name, old)


def get_merge_pair(workspace_id: uuid.UUID, merge_id: uuid.UUID) -> ContactMerge:
    found = ContactMerge.objects.for_workspace(workspace_id).filter(pk=merge_id).first()
    if found is None:
        raise NotFoundError("merge not found")
    return found


__all__ = ["get_contact", "get_merge_pair", "merge_contacts", "undo_merge"]
