"""Conversions API: tell Meta what became of a lead, but only for people who agreed to it."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

from django.db import IntegrityError, transaction

from core.errors import NotFoundError, RetryableProviderError, ValidationFailedError
from crm.consent import services as consent
from crm.contacts import services as contacts
from crm.leads.models import (
    ConversionLog,
    ConversionOutcome,
    ConversionRule,
    ConversionTrigger,
)
from crm.meta import services as meta

NAME_LIMIT = 100
EVENT_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,59}$")
ACTION_SOURCE = "system_generated"
MICROS = 1_000_000
NON_DIGITS = re.compile(r"\D")


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


# ---- rules ------------------------------------------------------------------------------------


def list_rules(workspace_id: uuid.UUID) -> list[ConversionRule]:
    return list(ConversionRule.objects.for_workspace(workspace_id).order_by("created_at"))


def get_rule(workspace_id: uuid.UUID, rule_id: uuid.UUID) -> ConversionRule:
    found = ConversionRule.objects.for_workspace(workspace_id).filter(pk=rule_id).first()
    if found is None:
        raise NotFoundError("conversion rule not found")
    return found


def create_rule(
    workspace_id: uuid.UUID,
    *,
    name: str,
    trigger: str,
    event_name: str,
    stage_id: uuid.UUID | None = None,
    send_value: bool = False,
) -> ConversionRule:
    if trigger not in ConversionTrigger.values or not name.strip() or len(name) > NAME_LIMIT:
        raise ValidationFailedError("invalid_rule")
    if not EVENT_NAME_PATTERN.match(event_name):
        raise ValidationFailedError("invalid_event_name")
    if (trigger == ConversionTrigger.STAGE_REACHED) != (stage_id is not None):
        raise ValidationFailedError("stage_needed_exactly_for_stage_rules")
    return ConversionRule.objects.create(
        workspace_id=workspace_id,
        name=name.strip(),
        trigger=trigger,
        event_name=event_name,
        stage_id=stage_id,
        send_value=send_value,
    )


def set_active(workspace_id: uuid.UUID, rule_id: uuid.UUID, active: bool) -> ConversionRule:
    rule = get_rule(workspace_id, rule_id)
    rule.is_active = active
    rule.save(update_fields=["is_active", "updated_at"])
    return rule


def delete_rule(workspace_id: uuid.UUID, rule_id: uuid.UUID) -> None:
    get_rule(workspace_id, rule_id).delete()


def recent_logs(workspace_id: uuid.UUID, limit: int = 50) -> list[ConversionLog]:
    rows = ConversionLog.objects.for_workspace(workspace_id).select_related("rule")
    return list(rows[: max(1, min(limit, 200))])


# ---- reporting --------------------------------------------------------------------------------


def report(
    workspace_id: uuid.UUID,
    *,
    trigger: str,
    source_event_id: str,
    contact_id: uuid.UUID,
    stage_id: uuid.UUID | None = None,
    value_micros: int = 0,
    currency: str = "",
    leadgen_id: str = "",
) -> int:
    """Send the events that rules ask for. Returns how many went out."""
    rules = ConversionRule.objects.for_workspace(workspace_id).filter(
        trigger=trigger, is_active=True
    )
    if trigger == ConversionTrigger.STAGE_REACHED:
        rules = rules.filter(stage_id=stage_id)
    sent = 0
    for rule in rules:
        event_id = f"{rule.pk}:{source_event_id}"
        if ConversionLog.objects.filter(rule=rule, event_id=event_id).exists():
            continue
        outcome, detail, payload = _prepare(
            workspace_id, rule, event_id, contact_id, value_micros, currency, leadgen_id
        )
        if payload is not None:
            outcome, detail = _deliver(workspace_id, payload)
        try:
            with transaction.atomic():
                ConversionLog.objects.create(
                    workspace_id=workspace_id,
                    rule=rule,
                    event_id=event_id,
                    contact_id=contact_id,
                    outcome=outcome,
                    detail=detail,
                )
        except IntegrityError:
            continue
        sent += outcome == ConversionOutcome.SENT
    return sent


def _prepare(
    workspace_id: uuid.UUID,
    rule: ConversionRule,
    event_id: str,
    contact_id: uuid.UUID,
    value_micros: int,
    currency: str,
    leadgen_id: str,
) -> tuple[str, str, dict[str, Any] | None]:
    if not consent.has_explicit_opt_in(workspace_id, contact_id):
        return ConversionOutcome.SKIPPED_NO_CONSENT, "", None
    contact = contacts.get_contact(workspace_id, contact_id)
    user: dict[str, Any] = {}
    if contact.email:
        user["em"] = [_hash(contact.email)]
    digits = NON_DIGITS.sub("", contact.phone)
    if digits:
        user["ph"] = [_hash(digits)]
    if leadgen_id:
        user["lead_id"] = leadgen_id
    if not user:
        return ConversionOutcome.SKIPPED_NO_DATA, "", None
    payload: dict[str, Any] = {
        "event_name": rule.event_name,
        "event_time": int(time.time()),
        "event_id": event_id,
        "action_source": ACTION_SOURCE,
        "user_data": user,
    }
    if rule.send_value and value_micros and currency:
        payload["custom_data"] = {"value": value_micros / MICROS, "currency": currency}
    return ConversionOutcome.SENT, "", payload


def _deliver(workspace_id: uuid.UUID, payload: dict[str, Any]) -> tuple[str, str]:
    try:
        meta.send_conversions(workspace_id, [payload])
    except meta.GraphError as exc:
        if exc.retryable:
            raise RetryableProviderError(exc.code, provider="meta") from exc
        return ConversionOutcome.FAILED, exc.code
    except ValidationFailedError as exc:
        return ConversionOutcome.FAILED, str(exc)[:200]
    return ConversionOutcome.SENT, ""
