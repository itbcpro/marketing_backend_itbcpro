"""Public surface of the inbox module. The send gateway and channel adapters use only this."""

from crm.inbox import accounts, selectors, stats
from crm.inbox.conversations import (
    add_comment,
    apply_status,
    assign,
    create_outbound,
    get_conversation,
    get_or_create,
    mark_read,
    mark_sent,
    mark_unread,
    mirror_outbound,
    set_failed,
    set_flags,
)
from crm.inbox.ingest import InboundMessage, IngestResult, ingest, resolve_contact

listing = selectors.listing
get_message = stats.get_message
outbound_count_since = stats.outbound_count_since
delivery_stats = stats.delivery_stats
messages_by_sender = stats.messages_by_sender
add_account = accounts.add_account
any_account = accounts.any_account
contact_of_outbound = accounts.contact_of_outbound
default_sender = accounts.default_sender
find_account = accounts.find_account
get_account = accounts.get_account
list_accounts = accounts.list_accounts
remove_account = accounts.remove_account
update_account_config = accounts.update_config
unread_ids = selectors.unread_ids

__all__ = [
    "InboundMessage",
    "IngestResult",
    "add_account",
    "add_comment",
    "any_account",
    "apply_status",
    "assign",
    "contact_of_outbound",
    "create_outbound",
    "default_sender",
    "delivery_stats",
    "find_account",
    "get_account",
    "get_conversation",
    "get_message",
    "get_or_create",
    "ingest",
    "list_accounts",
    "listing",
    "mark_read",
    "mark_sent",
    "mark_unread",
    "messages_by_sender",
    "mirror_outbound",
    "outbound_count_since",
    "remove_account",
    "resolve_contact",
    "set_failed",
    "set_flags",
    "unread_ids",
    "update_account_config",
]
