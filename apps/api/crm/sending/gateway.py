"""The send gateway: the only way a message leaves the CRM (ADR 0010).

Inbox replies, bulk sends, workflows, the API and AI agents all call ``send``. Every check lives
here so no path can skip one. A blocked message is stored with its reason, never silently dropped.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.models import ActorType
from core.errors import ValidationFailedError
from crm.consent import services as consent
from crm.consent.models import Channel, Purpose
from crm.contacts import services as contacts
from crm.contacts.models import Contact
from crm.inbox import services as inbox
from crm.inbox.models import ChannelAccount, Conversation, Direction, Message, MessageStatus
from crm.plans import services as plans
from crm.sending import domains, registry, suppression
from crm.sending.rules import ADDRESS_IDENTITY, REPLY_WINDOWS, TAG_WINDOWS, Block
from crm.sending.tasks import deliver
from crm.usage import rating
from crm.usage import services as usage
from crm.usage.units import category_of
from workspaces.models import Workspace

RATE_WINDOW = timedelta(minutes=1)
TEMPLATE_KIND = "template"  # an attachment of this kind marks a pre-approved template send
TAG_KIND = "tag"  # an attachment of this kind carries a Messenger message tag


@dataclass(frozen=True)
class SendOutcome:
    message: Message
    blocked: bool
    reason: str = ""


def _month_start() -> Any:
    return timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _outbound(workspace_id: uuid.UUID) -> Any:
    return (
        Message.objects.for_workspace(workspace_id)
        .filter(direction=Direction.OUTBOUND)
        .exclude(status=MessageStatus.BLOCKED)
    )


def _window_open(
    workspace_id: uuid.UUID, contact: Contact, channel: str, tagged: bool = False
) -> bool:
    window = (TAG_WINDOWS if tagged else REPLY_WINDOWS).get(channel, REPLY_WINDOWS.get(channel))
    if window is None:
        return True
    last = (
        Conversation.objects.for_workspace(workspace_id)
        .filter(contact=contact)
        .values_list("last_inbound_at", flat=True)
        .first()
    )
    return bool(last and timezone.now() - last < window)


@dataclass(frozen=True)
class Check:
    """Everything a rule may look at when deciding whether a send is allowed."""

    workspace_id: uuid.UUID
    contact: Contact
    channel: str
    purpose: str
    body: str
    actor: Actor
    approved: bool
    channel_account_id: uuid.UUID | None = None
    template: bool = False
    tagged: bool = False
    category: str = ""


def _kill_switch(c: Check) -> str:
    paused = Workspace.objects.filter(pk=c.workspace_id, writes_paused=True).exists()
    return Block.KILL_SWITCH if paused else ""


def _approval(c: Check) -> str:
    return Block.APPROVAL_REQUIRED if c.actor.type == ActorType.AGENT and not c.approved else ""


def _channel_and_body(c: Check) -> str:
    adapter = registry.adapter_for(c.channel)
    if adapter is None or c.channel not in ADDRESS_IDENTITY:
        return Block.NO_CHANNEL
    too_long = len(c.body) > adapter.capabilities.max_body_length
    return Block.INVALID_BODY if not c.body.strip() or too_long else ""


def _sender_domain(c: Check) -> str:
    """Mail may leave only from a domain whose DNS has been verified."""
    if c.channel != Channel.EMAIL or c.channel_account_id is None:
        return ""
    account = (
        ChannelAccount.objects.for_workspace(c.workspace_id).filter(pk=c.channel_account_id).first()
    )
    if account is None:
        return Block.NO_CHANNEL
    domain = account.external_id.rsplit("@", 1)[-1]
    return "" if domains.is_verified(c.workspace_id, domain) else Block.DOMAIN_UNVERIFIED


def _address(c: Check) -> str:
    found = contacts.identity_value(c.workspace_id, c.contact.pk, ADDRESS_IDENTITY[c.channel])
    return "" if found else Block.NO_ADDRESS


def _suppressed(c: Check) -> str:
    address = contacts.identity_value(c.workspace_id, c.contact.pk, ADDRESS_IDENTITY[c.channel])
    listed = suppression.is_suppressed(c.workspace_id, c.channel, address)
    return Block.SUPPRESSED if listed else ""


def _consent(c: Check) -> str:
    decision = consent.decide(
        workspace_id=c.workspace_id, contact_id=c.contact.pk, channel=c.channel, purpose=c.purpose
    )
    return decision.reason if not decision.allowed else ""


def _window(c: Check) -> str:
    """Approved templates may open a conversation; free text only inside the reply window."""
    if c.template or _window_open(c.workspace_id, c.contact, c.channel, c.tagged):
        return ""
    return Block.WINDOW_CLOSED


def _plan_limit(c: Check) -> str:
    cap = plans.limit_for(c.workspace_id, plans.MESSAGES)
    if cap is None:
        return ""
    used = _outbound(c.workspace_id).filter(created_at__gte=_month_start()).count()
    return Block.LIMIT_REACHED if used >= cap else ""


def _money(c: Check) -> str:
    """Prepaid wallets must cover the message; an optional daily cap limits spend."""
    country = rating.country_of(c.contact.phone, c.contact.country)
    _, _, price, _ = usage.quote(
        workspace_id=c.workspace_id,
        channel=c.channel,
        body=c.body,
        country=country,
        category=c.category,
    )
    if not usage.can_afford(workspace_id=c.workspace_id, price_micros=price):
        return Block.INSUFFICIENT_FUNDS
    cap = usage.daily_spend_cap(c.workspace_id)
    if cap is not None and usage.spent_today(c.workspace_id) + price > cap:
        return Block.SPEND_CAP
    return ""


def _rate(c: Check) -> str:
    recent = _outbound(c.workspace_id).filter(
        channel=c.channel, created_at__gte=timezone.now() - RATE_WINDOW
    )
    return Block.RATE_LIMITED if recent.count() >= settings.CRM_SEND_PER_MINUTE else ""


# Order matters: cheap and absolute rules first, counters last.
CHECKS: tuple[Callable[[Check], str], ...] = (
    _kill_switch,
    _approval,
    _channel_and_body,
    _sender_domain,
    _address,
    _suppressed,
    _consent,
    _window,
    _plan_limit,
    _money,
    _rate,
)


def evaluate(
    *,
    workspace_id: uuid.UUID,
    contact: Contact,
    channel: str,
    purpose: str,
    body: str,
    actor: Actor,
    approved: bool,
    channel_account_id: uuid.UUID | None = None,
    template: bool = False,
    tagged: bool = False,
    category: str = "",
) -> str:
    """The reason this send must not happen, or an empty string when it may."""
    check = Check(
        workspace_id,
        contact,
        channel,
        purpose,
        body,
        actor,
        approved,
        channel_account_id,
        template,
        tagged,
        category,
    )
    return next((reason for rule in CHECKS if (reason := rule(check))), "")


def send(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    contact_id: uuid.UUID,
    channel: str,
    body: str,
    subject: str = "",
    purpose: str = Purpose.TRANSACTIONAL,
    attachments: list[dict[str, Any]] | None = None,
    channel_account_id: uuid.UUID | None = None,
    idempotency_key: str = "",
    approved: bool = False,
    options: dict[str, Any] | None = None,
) -> SendOutcome:
    """Check, record and queue one message. Safe to call again with the same idempotency key.

    ``approved`` is set only by the action layer after a person approved an agent's message.
    """
    if idempotency_key:
        previous = (
            Message.objects.for_workspace(workspace_id)
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if previous is not None:
            return SendOutcome(
                previous, previous.status == MessageStatus.BLOCKED, previous.error_code
            )
    if channel not in Channel.values or purpose not in Purpose.values:
        raise ValidationFailedError(
            "invalid channel or purpose", errors={"channel": "invalid_value"}
        )
    contact = contacts.get_contact(workspace_id, contact_id)
    kinds = {a.get("kind") for a in attachments or []}
    reason = evaluate(
        workspace_id=workspace_id,
        contact=contact,
        channel=channel,
        purpose=purpose,
        body=body,
        actor=actor,
        approved=approved,
        channel_account_id=channel_account_id,
        template=TEMPLATE_KIND in kinds,
        tagged=TAG_KIND in kinds,
        category=category_of(channel, attachments),
    )
    with transaction.atomic():
        message = inbox.create_outbound(
            workspace_id=workspace_id,
            actor=actor,
            contact=contact,
            channel=channel,
            body=body,
            subject=subject,
            attachments=attachments,
            channel_account_id=channel_account_id,
            status=MessageStatus.BLOCKED if reason else MessageStatus.PENDING,
            error_code=reason,
            idempotency_key=idempotency_key,
            options=options,
        )
        if not reason:
            transaction.on_commit(
                lambda: deliver.delay(workspace_id=str(workspace_id), message_id=str(message.pk))
            )
    return SendOutcome(message, bool(reason), reason)
