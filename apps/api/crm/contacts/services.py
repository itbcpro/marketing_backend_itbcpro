"""Public surface of the contacts module. Other modules import from here only."""

import uuid

from core.errors import NotFoundError
from crm.contacts import selectors, stats, tags
from crm.contacts.companies import create_company, delete_company, get_company, update_company
from crm.contacts.contact_service import (
    add_follower,
    assign,
    assign_round_robin,
    create_contact,
    delete_contact,
    erase_contact,
    get_contact,
    get_settings,
    remove_follower,
    restore_contact,
    update_contact,
    upsert_contact,
    validate_contact_data,
)
from crm.contacts.filters import compile_filter
from crm.contacts.identities import attach as attach_identity
from crm.contacts.identities import find_contact
from crm.contacts.identities import value_of as identity_value
from crm.contacts.merge import merge_contacts, undo_merge
from crm.contacts.models import Contact
from workspaces.models import Membership
from workspaces.scope import can_see

add_tags = tags.add_tags
remove_tags = tags.remove_tags
list_tags = tags.list_tags
merge_tags = tags.merge_into
rename_tag = tags.rename
tag_names_of = tags.names_of
query_contacts = selectors.query
serialise_contacts = selectors.serialise


def get_visible_contact(membership: Membership, contact_id: uuid.UUID) -> Contact:
    """The contact if the member may see it (workspace and record scope), else not found."""
    contact = selectors.get_visible(membership, contact_id)
    if contact is None or not can_see(membership, contact.assigned_to_id):
        raise NotFoundError("contact not found")
    return contact


new_contacts_by_source = stats.new_contacts_by_source
count_active = stats.count_active


__all__ = [
    "add_follower",
    "add_tags",
    "assign",
    "assign_round_robin",
    "attach_identity",
    "compile_filter",
    "count_active",
    "create_company",
    "create_contact",
    "delete_company",
    "delete_contact",
    "erase_contact",
    "find_contact",
    "get_company",
    "get_contact",
    "get_settings",
    "get_visible_contact",
    "identity_value",
    "list_tags",
    "merge_contacts",
    "merge_tags",
    "new_contacts_by_source",
    "query_contacts",
    "remove_follower",
    "remove_tags",
    "rename_tag",
    "restore_contact",
    "serialise_contacts",
    "tag_names_of",
    "undo_merge",
    "update_company",
    "update_contact",
    "upsert_contact",
    "validate_contact_data",
]
