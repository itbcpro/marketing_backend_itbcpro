"""Event data models of the contacts module."""

import uuid

from pydantic import BaseModel

from crm.events.registry import event


class ContactRef(BaseModel):
    contact_id: uuid.UUID


@event("crm.contact.created")
class ContactCreated(ContactRef):
    source: str = ""


@event("crm.contact.updated")
class ContactUpdated(ContactRef):
    changed: list[str]


@event("crm.contact.deleted")
class ContactDeleted(ContactRef):
    pass


@event("crm.contact.restored")
class ContactRestored(ContactRef):
    pass


@event("crm.contact.erased")
class ContactErased(ContactRef):
    pass


@event("crm.contact.assigned")
class ContactAssigned(ContactRef):
    assigned_to: uuid.UUID | None


@event("crm.contact.merged")
class ContactMerged(BaseModel):
    merge_id: uuid.UUID
    winner_id: uuid.UUID
    loser_id: uuid.UUID


@event("crm.contact.merge_undone")
class ContactMergeUndone(BaseModel):
    merge_id: uuid.UUID
    winner_id: uuid.UUID
    loser_id: uuid.UUID


@event("crm.tag.added")
class TagAdded(ContactRef):
    tag_id: uuid.UUID
    name: str


@event("crm.tag.removed")
class TagRemoved(ContactRef):
    tag_id: uuid.UUID
    name: str
