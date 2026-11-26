"""Channel rules as data: which address a channel uses and how long a reply window stays open."""

from __future__ import annotations

from datetime import timedelta

from crm.consent.models import Channel
from crm.contacts.models import IdentityChannel

# Identity channel that holds the address of each messaging channel.
ADDRESS_IDENTITY: dict[str, str] = {
    Channel.EMAIL: IdentityChannel.EMAIL,
    Channel.SMS: IdentityChannel.PHONE,
    Channel.VOICE: IdentityChannel.PHONE,
    Channel.WHATSAPP: IdentityChannel.PHONE,
    Channel.INSTAGRAM: IdentityChannel.INSTAGRAM,
    Channel.MESSENGER: IdentityChannel.MESSENGER,
}

# Channels where free-form messages are allowed only for a while after the person last wrote.
REPLY_WINDOWS: dict[str, timedelta] = {
    Channel.WHATSAPP: timedelta(hours=24),
    Channel.INSTAGRAM: timedelta(hours=24),
    Channel.MESSENGER: timedelta(hours=24),
}


# Messenger lets a person reply to a customer for longer when the message carries a human-agent tag.
TAG_WINDOWS: dict[str, timedelta] = {Channel.MESSENGER: timedelta(days=7)}


class Block:
    """Machine codes for a blocked send; they are stored on the message and shown to people."""

    KILL_SWITCH = "kill_switch"
    APPROVAL_REQUIRED = "approval_required"
    NO_CHANNEL = "channel_unavailable"
    NO_ADDRESS = "no_address"
    WINDOW_CLOSED = "window_closed"
    LIMIT_REACHED = "limit_reached"
    SUPPRESSED = "suppressed"
    DOMAIN_UNVERIFIED = "domain_unverified"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    SPEND_CAP = "spend_cap_reached"
    RATE_LIMITED = "rate_limited"
    INVALID_BODY = "invalid_body"
