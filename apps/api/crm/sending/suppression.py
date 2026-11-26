"""Suppression list: addresses that bounced hard, complained, or were blocked by hand."""

from __future__ import annotations

import uuid

from audit.actors import Actor
from audit.services import record_event
from core.errors import NotFoundError, ValidationFailedError
from crm.common.normalize import InvalidValueError, normalise_email
from crm.sending.models import Suppression, SuppressionReason

EMAIL = "email"


def _clean(channel: str, address: str) -> str:
    if channel != EMAIL:
        return address.strip()
    try:
        return normalise_email(address)
    except InvalidValueError as exc:
        raise ValidationFailedError("invalid address", errors={"address": str(exc)}) from exc


def suppress(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    channel: str,
    address: str,
    reason: str,
    note: str = "",
) -> tuple[Suppression, bool]:
    """Add an address. Adding one that is already listed keeps the first reason."""
    if reason not in SuppressionReason.values:
        raise ValidationFailedError("invalid reason", errors={"reason": "invalid_value"})
    clean = _clean(channel, address)
    row, created = Suppression.objects.get_or_create(
        workspace_id=workspace_id,
        channel=channel,
        address=clean,
        defaults={"reason": reason, "note": note[:300]},
    )
    if created:
        record_event(
            action="crm.suppression.added",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_suppression",
            target_id=row.pk,
            after={"channel": channel, "reason": reason},
        )
    return row, created


def is_suppressed(workspace_id: uuid.UUID, channel: str, address: str) -> bool:
    return (
        Suppression.objects.for_workspace(workspace_id)
        .filter(
            channel=channel,
            address=address.strip().lower() if channel == EMAIL else address.strip(),
        )
        .exists()
    )


def remove(*, workspace_id: uuid.UUID, actor: Actor, suppression_id: uuid.UUID) -> None:
    row = Suppression.objects.for_workspace(workspace_id).filter(pk=suppression_id).first()
    if row is None:
        raise NotFoundError("suppression not found")
    row.delete()
    record_event(
        action="crm.suppression.removed",
        actor=actor,
        workspace_id=workspace_id,
        target_type="crm_suppression",
        target_id=suppression_id,
    )
