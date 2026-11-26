"""Campaign use cases: draft, launch, send in batches, pause, cancel, statistics."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.actors import Actor
from audit.services import record_event
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.campaigns.models import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from crm.consent.models import Channel, Purpose
from crm.consent.services import Reason
from crm.contacts import services as contacts
from crm.inbox.models import Message
from crm.sending import gateway
from crm.sending.rules import Block
from crm.templates import services as templates
from crm.tracking import services as tracking
from workspaces.models import Membership

NAME_LIMIT = 120
MAX_BATCH_SIZE = 1000
MIN_INTERVAL_SECONDS = 10
UNSUBSCRIBE_FIELD = "{{system.unsubscribe_url}}"
# Reasons that say "not now" rather than "never": the recipient waits for a later batch.
DEFER_REASONS = frozenset({Block.RATE_LIMITED, Reason.QUIET_HOURS})
MARKETING_CHANNELS_NEEDING_UNSUBSCRIBE = frozenset({Channel.EMAIL})
MAX_VARIANT_PERCENT = 50
OPTION_KEYS = ("track_opens", "track_clicks", "variant_subject", "variant_body", "variant_percent")
PERCENT = 100
VARIANT_A, VARIANT_B = "A", "B"


def _extra_errors(channel: str, purpose: str, options: dict[str, Any]) -> dict[str, str]:
    """Problems with the A/B test and tracking choices."""
    errors: dict[str, str] = {}
    percent = int(options.get("variant_percent", 0))
    variant_subject = str(options.get("variant_subject", ""))
    variant_body = str(options.get("variant_body", ""))
    if not 0 <= percent <= MAX_VARIANT_PERCENT:
        errors["variant_percent"] = "out_of_range"
    elif percent and not (variant_subject.strip() or variant_body.strip()):
        errors["variant_subject"] = "variant_needs_a_difference"
    if (
        variant_body
        and purpose == Purpose.MARKETING
        and channel in MARKETING_CHANNELS_NEEDING_UNSUBSCRIBE
        and UNSUBSCRIBE_FIELD not in variant_body
    ):
        errors["variant_body"] = "unsubscribe_required"
    if (options.get("track_opens") or options.get("track_clicks")) and channel != Channel.EMAIL:
        errors["track_opens"] = "email_only"
    return errors


def _validate(
    workspace_id: uuid.UUID,
    *,
    name: str,
    channel: str,
    purpose: str,
    subject: str,
    body: str,
    audience: dict[str, Any],
    batch_size: int,
    interval: int,
    options: dict[str, Any] | None = None,
) -> None:
    options = options or {}
    errors: dict[str, str] = {}
    if not name.strip() or len(name.strip()) > NAME_LIMIT:
        errors["name"] = "required" if not name.strip() else "too_long"
    if channel not in Channel.values:
        errors["channel"] = "invalid_value"
    if purpose not in Purpose.values:
        errors["purpose"] = "invalid_value"
    if not body.strip():
        errors["body"] = "required"
    elif (
        purpose == Purpose.MARKETING
        and channel in MARKETING_CHANNELS_NEEDING_UNSUBSCRIBE
        and UNSUBSCRIBE_FIELD not in body
    ):
        errors["body"] = "unsubscribe_required"
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        errors["batch_size"] = "out_of_range"
    if interval < MIN_INTERVAL_SECONDS:
        errors["batch_interval_seconds"] = "out_of_range"
    errors |= _extra_errors(channel, purpose, options)
    if errors:
        raise ValidationFailedError("invalid campaign", errors=errors)
    problems = templates.check_references(workspace_id, subject, body)
    problems |= templates.check_references(
        workspace_id, str(options.get("variant_subject", "")), str(options.get("variant_body", ""))
    )
    if problems:
        raise ValidationFailedError("unknown merge fields", errors=problems)
    contacts.compile_filter(workspace_id, audience)


def create(
    *,
    membership: Membership,
    actor: Actor,
    name: str,
    channel: str,
    body: str = "",
    subject: str = "",
    purpose: str = Purpose.MARKETING,
    audience: dict[str, Any] | None = None,
    channel_account_id: uuid.UUID | None = None,
    template_id: uuid.UUID | None = None,
    batch_size: int | None = None,
    batch_interval_seconds: int | None = None,
    track_opens: bool = False,
    track_clicks: bool = False,
    variant_subject: str = "",
    variant_body: str = "",
    variant_percent: int = 0,
) -> Campaign:
    workspace_id = membership.workspace_id
    if template_id:
        source = templates.get(workspace_id, template_id)
        body, subject = body or source.body, subject or source.subject
    size = settings.CRM_CAMPAIGN_BATCH_SIZE if batch_size is None else batch_size
    interval = (
        settings.CRM_CAMPAIGN_BATCH_INTERVAL
        if batch_interval_seconds is None
        else batch_interval_seconds
    )
    spec = audience or {}
    _validate(
        workspace_id,
        name=name,
        channel=channel,
        purpose=purpose,
        subject=subject,
        body=body,
        audience=spec,
        batch_size=size,
        interval=interval,
        options={
            "track_opens": track_opens,
            "track_clicks": track_clicks,
            "variant_subject": variant_subject,
            "variant_body": variant_body,
            "variant_percent": variant_percent,
        },
    )
    with transaction.atomic():
        campaign = Campaign.objects.create(
            workspace_id=workspace_id,
            name=name.strip(),
            channel=channel,
            purpose=purpose,
            subject=subject,
            body=body,
            audience=spec,
            track_opens=track_opens,
            track_clicks=track_clicks,
            variant_subject=variant_subject,
            variant_body=variant_body,
            variant_percent=variant_percent,
            channel_account_id=channel_account_id,
            batch_size=size,
            batch_interval_seconds=interval,
            created_by_id=membership.user_id,
        )
        record_event(
            action="crm.campaign.created",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_campaign",
            target_id=campaign.pk,
        )
    return campaign


def get(workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    found = Campaign.objects.for_workspace(workspace_id).filter(pk=campaign_id).first()
    if found is None:
        raise NotFoundError("campaign not found")
    return found


def update(
    *, membership: Membership, actor: Actor, campaign_id: uuid.UUID, data: dict[str, Any]
) -> Campaign:
    campaign = get(membership.workspace_id, campaign_id)
    if campaign.status != CampaignStatus.DRAFT:
        raise ConflictError("only drafts can change", errors={"campaign": "not_a_draft"})
    values = {
        "name": campaign.name,
        "channel": campaign.channel,
        "purpose": campaign.purpose,
        "subject": campaign.subject,
        "body": campaign.body,
        "audience": campaign.audience,
        "batch_size": campaign.batch_size,
        "batch_interval_seconds": campaign.batch_interval_seconds,
        "track_opens": campaign.track_opens,
        "track_clicks": campaign.track_clicks,
        "variant_subject": campaign.variant_subject,
        "variant_body": campaign.variant_body,
        "variant_percent": campaign.variant_percent,
        **data,
    }
    _validate(
        membership.workspace_id,
        name=values["name"],
        channel=values["channel"],
        purpose=values["purpose"],
        subject=values["subject"],
        body=values["body"],
        audience=values["audience"],
        batch_size=values["batch_size"],
        interval=values["batch_interval_seconds"],
        options={k: values[k] for k in OPTION_KEYS},
    )
    for key, value in values.items():
        setattr(campaign, key, value)
    campaign.save()
    record_event(
        action="crm.campaign.updated",
        actor=actor,
        workspace_id=membership.workspace_id,
        target_type="crm_campaign",
        target_id=campaign.pk,
    )
    return campaign


# ---- lifecycle --------------------------------------------------------------------------------


def _creator_membership(campaign: Campaign) -> Membership | None:
    if campaign.created_by_id is None:
        return None
    return Membership.objects.filter(
        workspace_id=campaign.workspace_id, user_id=campaign.created_by_id
    ).first()


def _variant_of(campaign: Campaign, contact_id: uuid.UUID) -> str:
    """A fixed share of the audience gets B. The split is a function of the contact and the
    campaign, so a recipient is never moved between variants and a rerun gives the same split."""
    if not campaign.variant_percent:
        return VARIANT_A
    bucket = (contact_id.int ^ campaign.pk.int) % PERCENT
    return VARIANT_B if bucket < campaign.variant_percent else VARIANT_A


def _freeze_audience(campaign: Campaign) -> int:
    """Turn the audience filter into recipient rows, with the creator's own record scope."""
    membership = _creator_membership(campaign)
    if membership is None:
        raise ValidationFailedError("owner left", errors={"campaign": "owner_missing"})
    limit = settings.CRM_BULK_MAX_RECORDS
    ids = list(
        contacts.query_contacts(membership, spec=campaign.audience)
        .values_list("pk", flat=True)
        .order_by("pk")[: limit + 1]
    )
    if len(ids) > limit:
        raise ValidationFailedError("audience too large", errors={"audience": "too_many"})
    CampaignRecipient.objects.bulk_create(
        [
            CampaignRecipient(
                workspace_id=campaign.workspace_id,
                campaign=campaign,
                contact_id=i,
                variant=_variant_of(campaign, i),
            )
            for i in ids
        ],
        ignore_conflicts=True,
    )
    return len(ids)


def _begin(campaign: Campaign, now: datetime) -> None:
    total = _freeze_audience(campaign)
    campaign.started_at, campaign.next_batch_at = now, now
    campaign.status = CampaignStatus.SENDING if total else CampaignStatus.DONE
    if not total:
        campaign.finished_at = now
    campaign.save()


def launch(
    *,
    workspace_id: uuid.UUID,
    actor: Actor,
    campaign_id: uuid.UUID,
    scheduled_at: datetime | None = None,
) -> Campaign:
    now = timezone.now()
    with transaction.atomic():
        campaign = (
            Campaign.objects.select_for_update()
            .filter(workspace_id=workspace_id, pk=campaign_id)
            .first()
        )
        if campaign is None:
            raise NotFoundError("campaign not found")
        if campaign.status != CampaignStatus.DRAFT:
            raise ConflictError("already launched", errors={"campaign": "already_launched"})
        if scheduled_at is not None and scheduled_at > now:
            campaign.status, campaign.scheduled_at = CampaignStatus.SCHEDULED, scheduled_at
            campaign.save()
        else:
            _begin(campaign, now)
        record_event(
            action="crm.campaign.launched",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_campaign",
            target_id=campaign.pk,
            after={"status": campaign.status},
        )
    return campaign


def pause(*, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    campaign = get(workspace_id, campaign_id)
    if campaign.status != CampaignStatus.SENDING:
        raise ConflictError("not sending", errors={"campaign": "not_sending"})
    campaign.status = CampaignStatus.PAUSED
    campaign.save(update_fields=["status", "updated_at"])
    return campaign


def resume(*, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    campaign = get(workspace_id, campaign_id)
    if campaign.status != CampaignStatus.PAUSED:
        raise ConflictError("not paused", errors={"campaign": "not_paused"})
    campaign.status, campaign.next_batch_at = CampaignStatus.SENDING, timezone.now()
    campaign.save(update_fields=["status", "next_batch_at", "updated_at"])
    return campaign


def cancel(*, workspace_id: uuid.UUID, actor: Actor, campaign_id: uuid.UUID) -> Campaign:
    with transaction.atomic():
        campaign = (
            Campaign.objects.select_for_update()
            .filter(workspace_id=workspace_id, pk=campaign_id)
            .first()
        )
        if campaign is None:
            raise NotFoundError("campaign not found")
        if campaign.status in {CampaignStatus.DONE, CampaignStatus.CANCELED}:
            raise ConflictError("already finished", errors={"campaign": "already_finished"})
        campaign.status, campaign.finished_at = CampaignStatus.CANCELED, timezone.now()
        campaign.save()
        CampaignRecipient.objects.filter(campaign=campaign, status=RecipientStatus.PENDING).update(
            status=RecipientStatus.SKIPPED, reason="canceled", processed_at=timezone.now()
        )
        record_event(
            action="crm.campaign.canceled",
            actor=actor,
            workspace_id=workspace_id,
            target_type="crm_campaign",
            target_id=campaign.pk,
        )
    return campaign


# ---- sending ----------------------------------------------------------------------------------


def due_campaigns() -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """(workspace, campaign, what to do) for the periodic job."""
    now = timezone.now()
    due: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for ws, pk in Campaign.objects.filter(
        status=CampaignStatus.SCHEDULED, scheduled_at__lte=now
    ).values_list("workspace_id", "pk"):
        due.append((ws, pk, "start"))
    for ws, pk in Campaign.objects.filter(
        status=CampaignStatus.SENDING, next_batch_at__lte=now
    ).values_list("workspace_id", "pk"):
        due.append((ws, pk, "batch"))
    return due


def start_scheduled(*, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    now = timezone.now()
    with transaction.atomic():
        campaign = (
            Campaign.objects.select_for_update()
            .filter(workspace_id=workspace_id, pk=campaign_id)
            .first()
        )
        if campaign is None or campaign.status != CampaignStatus.SCHEDULED:
            raise NotFoundError("campaign not scheduled")
        try:
            _begin(campaign, now)
        except ValidationFailedError:  # the owner left or the audience grew too large
            campaign.status, campaign.finished_at = CampaignStatus.CANCELED, now
            campaign.save()
    return campaign


def _claim(workspace_id: uuid.UUID, campaign_id: uuid.UUID, now: datetime) -> Campaign | None:
    """Reserve the next batch. A crashed worker's reservation simply expires and is retried."""
    with transaction.atomic():
        campaign = (
            Campaign.objects.select_for_update()
            .filter(workspace_id=workspace_id, pk=campaign_id)
            .first()
        )
        if (
            campaign is None
            or campaign.status != CampaignStatus.SENDING
            or (campaign.next_batch_at and campaign.next_batch_at > now)
        ):
            return None
        lease = max(campaign.batch_interval_seconds * 2, settings.CRM_CAMPAIGN_DEFER_SECONDS)
        campaign.next_batch_at = now + timedelta(seconds=lease)
        campaign.save(update_fields=["next_batch_at", "updated_at"])
        return campaign


def _send_one(campaign: Campaign, recipient: CampaignRecipient) -> bool:
    """Send to one recipient. Returns False when the whole batch should wait."""
    actor = (
        Actor.human(campaign.created_by_id) if campaign.created_by_id else Actor.system("campaign")
    )
    try:
        contact = contacts.get_contact(campaign.workspace_id, recipient.contact_id)
        subject, body = campaign.subject, campaign.body
        if recipient.variant == VARIANT_B:
            subject = campaign.variant_subject or subject
            body = campaign.variant_body or body
        rendered = templates.render_text(
            campaign.workspace_id, contact.pk, subject=subject, body=body
        )
    except NotFoundError:
        recipient.status, recipient.reason = RecipientStatus.SKIPPED, "contact_gone"
        return True
    reason = gateway.evaluate(
        workspace_id=campaign.workspace_id,
        contact=contact,
        channel=campaign.channel,
        purpose=campaign.purpose,
        body=rendered.body,
        actor=actor,
        approved=False,
        channel_account_id=campaign.channel_account_id,
    )
    if reason in DEFER_REASONS:
        return False
    outcome = gateway.send(
        workspace_id=campaign.workspace_id,
        actor=actor,
        contact_id=contact.pk,
        channel=campaign.channel,
        body=rendered.body,
        subject=rendered.subject,
        purpose=campaign.purpose,
        channel_account_id=campaign.channel_account_id,
        idempotency_key=f"campaign:{campaign.pk}:{contact.pk}",
        options={
            tracking.OPTION_OPENS: campaign.track_opens,
            tracking.OPTION_CLICKS: campaign.track_clicks,
        },
    )
    recipient.status = RecipientStatus.BLOCKED if outcome.blocked else RecipientStatus.SENT
    recipient.reason, recipient.message_id = outcome.reason, outcome.message.pk
    return True


def process_batch(*, workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign | None:
    """Send the next batch. A claim and per-recipient idempotency keys make a repeat harmless."""
    now = timezone.now()
    campaign = _claim(workspace_id, campaign_id, now)
    if campaign is None:
        return None
    pending = list(
        CampaignRecipient.objects.filter(campaign=campaign, status=RecipientStatus.PENDING)
        .select_related("contact")
        .order_by("pk")[: campaign.batch_size]
    )
    deferred = False
    for recipient in pending:
        if not _send_one(campaign, recipient):
            deferred = True
            break
        recipient.processed_at = timezone.now()
        recipient.save()
    left = CampaignRecipient.objects.filter(
        campaign=campaign, status=RecipientStatus.PENDING
    ).exists()
    campaign.refresh_from_db()
    if campaign.status != CampaignStatus.SENDING:
        return campaign  # paused or canceled while this batch ran
    if not left:
        campaign.status, campaign.finished_at = CampaignStatus.DONE, timezone.now()
    else:
        wait = settings.CRM_CAMPAIGN_DEFER_SECONDS if deferred else campaign.batch_interval_seconds
        campaign.next_batch_at = timezone.now() + timedelta(seconds=wait)
    campaign.save()
    return campaign


# ---- numbers ----------------------------------------------------------------------------------


def stats(workspace_id: uuid.UUID, campaign_id: uuid.UUID) -> dict[str, Any]:
    campaign = get(workspace_id, campaign_id)
    rows = CampaignRecipient.objects.filter(campaign=campaign)
    by_status = Counter(rows.values_list("status", flat=True))
    blocked = Counter(rows.filter(status=RecipientStatus.BLOCKED).values_list("reason", flat=True))
    message_ids = rows.exclude(message__isnull=True).values_list("message_id", flat=True)
    delivery = Counter(
        Message.objects.filter(pk__in=list(message_ids)).values_list("status", flat=True)
    )
    return {
        "total": sum(by_status.values()),
        "recipients": {s: by_status.get(s, 0) for s in RecipientStatus.values},
        "blocked_reasons": dict(blocked),
        "delivery": dict(delivery),
        "variants": _variant_stats(campaign, rows),
    }


def _variant_stats(campaign: Campaign, rows: Any) -> list[dict[str, Any]]:
    """Per variant: how many were sent, delivered, opened and clicked (each person once)."""
    out = []
    for name in (VARIANT_A, VARIANT_B):
        sent_rows = rows.filter(variant=name, status=RecipientStatus.SENT).exclude(
            message__isnull=True
        )
        ids = list(sent_rows.values_list("message_id", flat=True))
        if name == VARIANT_B and not campaign.variant_percent:
            continue
        delivered = Message.objects.filter(pk__in=ids, status__in=["delivered", "read"]).count()
        seen = tracking.counts_for_messages(campaign.workspace_id, ids)
        out.append(
            {
                "variant": name,
                "recipients": rows.filter(variant=name).count(),
                "sent": len(ids),
                "delivered": delivered,
                "opened": seen["opened"],
                "clicked": seen["clicked"],
            }
        )
    return out
