"""Consent use cases and the single decision every outbound message must pass."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ValidationFailedError
from crm.consent import events
from crm.consent.keywords import KeywordAction, classify
from crm.consent.models import (
    Channel,
    ConsentEvent,
    ConsentSettings,
    ConsentState,
    Purpose,
    Source,
    Status,
)
from crm.consent.profiles import LawProfile, QuietHours, get_profile
from crm.contacts import services as contacts
from crm.events.publisher import publish
from workspaces.models import Workspace

PURPOSES = tuple(Purpose.values)


class Reason:
    """Machine codes returned when a send is blocked; the gateway stores them on the message."""

    OPTED_OUT = "opted_out"
    NO_CONSENT = "no_consent"
    CONSENT_EXPIRED = "consent_expired"
    QUIET_HOURS = "quiet_hours"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


def get_settings(workspace_id: uuid.UUID) -> ConsentSettings:
    row, _ = ConsentSettings.objects.get_or_create(workspace_id=workspace_id)
    return row


def set_law_profile(
    *, workspace_id: uuid.UUID, profile: str, quiet_hours: bool | None = None
) -> ConsentSettings:
    get_profile(profile)  # raises for unknown keys
    row = get_settings(workspace_id)
    row.law_profile = profile
    if quiet_hours is not None:
        row.quiet_hours_enabled = quiet_hours
    row.save()
    return row


def record(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    contact_id: uuid.UUID,
    channel: str,
    purpose: str,
    status: str,
    source: str,
    evidence: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    occurred_at: datetime | None = None,
) -> ConsentEvent:
    """Append a decision to the ledger and update the derived state. Always accepted."""
    errors = {
        name: "invalid_value"
        for name, value, allowed in (
            ("channel", channel, Channel.values),
            ("purpose", purpose, Purpose.values),
            ("status", status, Status.values),
            ("source", source, Source.values),
        )
        if value not in allowed
    }
    if errors:
        raise ValidationFailedError("invalid consent", errors=errors)
    with transaction.atomic():
        contact = contacts.get_contact(workspace_id, contact_id)
        entry = ConsentEvent.objects.create(
            workspace_id=workspace_id,
            contact=contact,
            channel=channel,
            purpose=purpose,
            status=status,
            source=source,
            evidence=evidence or {},
            expires_at=expires_at,
            actor_type=actor.type,
            actor_id=actor.id,
            occurred_at=occurred_at or timezone.now(),
        )
        state = (
            ConsentState.objects.select_for_update()
            .filter(contact=contact, channel=channel, purpose=purpose)
            .first()
        )
        previous = state.status if state else None
        if state is None:
            state = ConsentState(
                workspace_id=workspace_id, contact=contact, channel=channel, purpose=purpose
            )
        state.status, state.expires_at = status, expires_at
        state.last_event, state.changed_at = entry, entry.occurred_at
        state.save()
        if previous != status:
            record_event(
                action="crm.consent.changed",
                actor=actor,
                workspace_id=workspace_id,
                target_type="crm_contact",
                target_id=contact.pk,
                before={"status": previous},
                after={"status": status, "channel": channel, "purpose": purpose},
            )
            publish(
                "crm.consent.changed",
                workspace_id=workspace_id,
                actor=actor,
                data=events.ConsentChanged(
                    contact_id=contact.pk,
                    channel=channel,
                    purpose=purpose,
                    status=status,
                    source=source,
                ),
            )
    return entry


def states_of(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[ConsentState]:
    return list(
        ConsentState.objects.for_workspace(workspace_id)
        .filter(contact_id=contact_id)
        .order_by("channel", "purpose")
    )


def history(
    workspace_id: uuid.UUID, contact_id: uuid.UUID, channel: str | None = None
) -> list[ConsentEvent]:
    rows = ConsentEvent.objects.for_workspace(workspace_id).filter(contact_id=contact_id)
    return list(rows.filter(channel=channel) if channel else rows)


def evidence_export(workspace_id: uuid.UUID, contact_id: uuid.UUID) -> list[dict[str, Any]]:
    """Everything a regulator may ask for about one contact, oldest first."""
    return [
        {
            "id": str(e.pk),
            "channel": e.channel,
            "purpose": e.purpose,
            "status": e.status,
            "source": e.source,
            "evidence": e.evidence,
            "actor": f"{e.actor_type}:{e.actor_id}",
            "occurred_at": e.occurred_at.isoformat(),
            "expires_at": e.expires_at.isoformat() if e.expires_at else None,
        }
        for e in reversed(history(workspace_id, contact_id))
    ]


# ---- the decision -----------------------------------------------------------------------------


def _in_quiet_hours(quiet: QuietHours, local: datetime) -> bool:
    now = local.time()
    if quiet.start <= quiet.end:
        return quiet.start <= now < quiet.end
    return now >= quiet.start or now < quiet.end


def _zone(workspace_id: uuid.UUID, time_zone: str) -> ZoneInfo:
    name = time_zone or Workspace.objects.values_list("time_zone", flat=True).get(pk=workspace_id)
    return ZoneInfo(name)


def decide(
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
    channel: str,
    purpose: str,
    now: datetime | None = None,
) -> Decision:
    """May we send this purpose on this channel to this contact right now?"""
    now = now or timezone.now()
    settings_row = get_settings(workspace_id)
    profile = get_profile(settings_row.law_profile)
    state = (
        ConsentState.objects.for_workspace(workspace_id)
        .filter(contact_id=contact_id, channel=channel, purpose=purpose)
        .first()
    )
    verdict = _consent_verdict(profile, state, channel, purpose, now)
    if not verdict.allowed:
        return verdict
    quiet = profile.quiet_hours.get(channel)
    if settings_row.quiet_hours_enabled and quiet and purpose == Purpose.MARKETING:
        contact = contacts.get_contact(workspace_id, contact_id)
        local = now.astimezone(_zone(workspace_id, contact.time_zone))
        if _in_quiet_hours(quiet, local):
            return Decision(False, Reason.QUIET_HOURS)
    return Decision(True)


def _consent_verdict(
    profile: LawProfile, state: ConsentState | None, channel: str, purpose: str, now: datetime
) -> Decision:
    if state is not None and state.status == Status.OPTED_OUT:
        return Decision(False, Reason.OPTED_OUT)
    needs_opt_in = purpose == Purpose.MARKETING and channel in profile.opt_in_required
    if not needs_opt_in:
        return Decision(True)
    if state is None:
        return Decision(False, Reason.NO_CONSENT)
    if state.expires_at is not None and state.expires_at <= now:
        return Decision(False, Reason.CONSENT_EXPIRED)
    return Decision(True)


# ---- inbound keywords ---------------------------------------------------------------------------


def handle_keyword(
    *, workspace_id: uuid.UUID, contact_id: uuid.UUID, channel: str, text: str, message_id: str = ""
) -> KeywordAction | None:
    """Apply STOP, START and HELP style messages. Returns the action so the caller can reply."""
    action = classify(text)
    if action is None or action == KeywordAction.HELP:
        return action
    status = Status.OPTED_OUT if action == KeywordAction.OPT_OUT else Status.OPTED_IN
    with transaction.atomic():
        for purpose in PURPOSES:
            record(
                workspace_id=workspace_id,
                actor=Actor.system("keyword"),
                contact_id=contact_id,
                channel=channel,
                purpose=purpose,
                status=status,
                source=Source.KEYWORD,
                evidence={"text": text.strip()[:100], "message_id": message_id},
            )
    return action


def has_explicit_opt_in(
    workspace_id: uuid.UUID, contact_id: uuid.UUID, purpose: str = Purpose.MARKETING
) -> bool:
    """True when the contact said yes on at least one channel and has not said no since.

    Unlike ``decide`` this ignores law profiles that allow sending without a yes: it answers
    "did this person agree?", which is what sharing data with an ad platform needs.
    """
    now = timezone.now()
    rows = ConsentState.objects.for_workspace(workspace_id).filter(
        contact_id=contact_id, purpose=purpose
    )
    states = list(rows)
    if any(s.status == Status.OPTED_OUT for s in states):
        return False
    return any(
        s.status == Status.OPTED_IN and (s.expires_at is None or s.expires_at > now) for s in states
    )
