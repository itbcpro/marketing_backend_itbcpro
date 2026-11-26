"""Signed unsubscribe links (RFC 8058 one-click) that turn into a consent opt-out."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core import signing

from audit.actors import Actor
from core.errors import NotFoundError
from crm.consent import services as consent
from crm.consent.models import Channel, Purpose, Source, Status
from crm.contacts import services as contacts

SALT = "crm.unsubscribe"
PATH = "/bff/api/v1/crm-public/unsubscribe"
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class Target:
    workspace_id: uuid.UUID
    contact_id: uuid.UUID
    channel: str


def make_token(workspace_id: uuid.UUID, contact_id: uuid.UUID, channel: str = Channel.EMAIL) -> str:
    return signing.dumps(
        {"w": str(workspace_id), "c": str(contact_id), "ch": str(channel)}, salt=SALT, compress=True
    )


def url_for(workspace_id: uuid.UUID, contact_id: uuid.UUID, channel: str = Channel.EMAIL) -> str:
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}{PATH}/{make_token(workspace_id, contact_id, channel)}"


def page_url_for(
    workspace_id: uuid.UUID, contact_id: uuid.UUID, channel: str = Channel.EMAIL
) -> str:
    """The human page behind the link, for message footers (the header uses the API address)."""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/unsubscribe/{make_token(workspace_id, contact_id, channel)}"


def read_token(token: str) -> Target:
    """Verify a link. Any problem looks the same to the caller (not found)."""
    max_age = settings.CRM_UNSUBSCRIBE_MAX_AGE_DAYS * SECONDS_PER_DAY
    try:
        data = signing.loads(token, salt=SALT, max_age=max_age)
        return Target(uuid.UUID(data["w"]), uuid.UUID(data["c"]), str(data["ch"]))
    except (signing.BadSignature, KeyError, ValueError) as exc:
        raise NotFoundError("link not valid") from exc


def mask(address: str) -> str:
    name, _, domain = address.partition("@")
    return f"{name[:1]}***@{domain}" if domain else "***"


def current_address(target: Target) -> str:
    try:
        return contacts.get_contact(target.workspace_id, target.contact_id).email
    except NotFoundError:
        return ""


def is_unsubscribed(target: Target) -> bool:
    states = consent.states_of(target.workspace_id, target.contact_id)
    return any(
        s.channel == target.channel
        and s.purpose == Purpose.MARKETING
        and s.status == Status.OPTED_OUT
        for s in states
    )


def unsubscribe(target: Target) -> None:
    """Opt the contact out of marketing on the channel. Repeating it changes nothing."""
    if is_unsubscribed(target):
        return
    try:
        consent.record(
            workspace_id=target.workspace_id,
            actor=Actor.system("unsubscribe"),
            contact_id=target.contact_id,
            channel=target.channel,
            purpose=Purpose.MARKETING,
            status=Status.OPTED_OUT,
            source=Source.LINK,
            evidence={"via": "unsubscribe_link"},
        )
    except NotFoundError:
        return  # the contact no longer exists, so there is nothing left to stop
