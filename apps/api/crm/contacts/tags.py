"""Tags: free-form labels on contacts, the main segmentation primitive."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import transaction
from django.db.models.functions import Lower

from audit.actors import Actor
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.contacts import events
from crm.contacts.models import Contact, ContactTag, Tag
from crm.events.publisher import publish

NAME_LIMIT = 100
COLOR_LENGTH = 7


def _clean_names(names: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    if len(cleaned) > settings.CRM_MAX_TAGS_PER_REQUEST:
        raise ValidationFailedError("too many tags", errors={"tags": "too_many"})
    if any(len(n) > NAME_LIMIT for n in cleaned):
        raise ValidationFailedError("tag too long", errors={"tags": "too_long"})
    return cleaned


def get_or_create(workspace_id: uuid.UUID, name: str) -> Tag:
    existing = (
        Tag.objects.for_workspace(workspace_id)
        .annotate(low=Lower("name"))
        .filter(low=name.lower())
        .first()
    )
    return existing or Tag.objects.create(workspace_id=workspace_id, name=name)


def add_tags(
    *, workspace_id: uuid.UUID, actor: Actor, contact: Contact, names: list[str]
) -> list[Tag]:
    """Attach tags by name (created on first use). Existing links are left alone."""
    added: list[Tag] = []
    with transaction.atomic():
        for name in _clean_names(names):
            tag = get_or_create(workspace_id, name)
            _, created = ContactTag.objects.get_or_create(
                workspace_id=workspace_id, contact=contact, tag=tag
            )
            if created:
                added.append(tag)
                publish(
                    "crm.tag.added",
                    workspace_id=workspace_id,
                    actor=actor,
                    data=events.TagAdded(contact_id=contact.pk, tag_id=tag.pk, name=tag.name),
                )
    return added


def remove_tags(
    *, workspace_id: uuid.UUID, actor: Actor, contact: Contact, names: list[str]
) -> list[Tag]:
    removed: list[Tag] = []
    with transaction.atomic():
        wanted = {n.lower() for n in _clean_names(names)}
        links = (
            ContactTag.objects.for_workspace(workspace_id)
            .filter(contact=contact)
            .select_related("tag")
        )
        for link in links:
            if link.tag.name.lower() in wanted:
                removed.append(link.tag)
                link.delete()
                publish(
                    "crm.tag.removed",
                    workspace_id=workspace_id,
                    actor=actor,
                    data=events.TagRemoved(
                        contact_id=contact.pk, tag_id=link.tag_id, name=link.tag.name
                    ),
                )
    return removed


def names_of(contact_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    out: dict[uuid.UUID, list[str]] = {}
    for contact_id, name in (
        ContactTag.objects.filter(contact_id__in=contact_ids)
        .order_by("tag__name")
        .values_list("contact_id", "tag__name")
    ):
        out.setdefault(contact_id, []).append(name)
    return out


def rename(*, workspace_id: uuid.UUID, tag_id: uuid.UUID, name: str) -> Tag:
    new_name = _clean_names([name])
    if not new_name:
        raise ValidationFailedError("name required", errors={"name": "required"})
    with transaction.atomic():
        tag = Tag.objects.for_workspace(workspace_id).select_for_update().filter(pk=tag_id).first()
        if tag is None:
            raise NotFoundError("tag not found")
        clash = (
            Tag.objects.for_workspace(workspace_id)
            .annotate(low=Lower("name"))
            .filter(low=new_name[0].lower())
            .exclude(pk=tag.pk)
            .exists()
        )
        if clash:
            raise ConflictError("tag name already exists", errors={"name": "duplicate"})
        tag.name = new_name[0]
        tag.save(update_fields=["name", "updated_at"])
    return tag


def merge_into(*, workspace_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID) -> Tag:
    """Move every link of ``source`` to ``target`` without duplicates, then delete ``source``."""
    if source_id == target_id:
        raise ValidationFailedError("same tag", errors={"tag": "same_tag"})
    with transaction.atomic():
        scoped = Tag.objects.for_workspace(workspace_id)
        source, target = scoped.filter(pk=source_id).first(), scoped.filter(pk=target_id).first()
        if source is None or target is None:
            raise NotFoundError("tag not found")
        have = set(ContactTag.objects.filter(tag=target).values_list("contact_id", flat=True))
        for link in ContactTag.objects.filter(tag=source):
            if link.contact_id in have:
                link.delete()
            else:
                link.tag = target
                link.save(update_fields=["tag", "updated_at"])
        source.delete()
    return target


def list_tags(workspace_id: uuid.UUID) -> list[Tag]:
    return list(Tag.objects.for_workspace(workspace_id))
