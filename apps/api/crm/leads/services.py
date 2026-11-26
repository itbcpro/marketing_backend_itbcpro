"""Lead capture and form mappings (public surface of the module)."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import IntegrityError, transaction

from audit.actors import Actor
from core.errors import ConflictError, NotFoundError, ValidationFailedError
from crm.consent import services as consent
from crm.consent.models import Channel, Purpose, Source, Status
from crm.contacts import services as contacts
from crm.events.publisher import publish
from crm.leads import events, mapping
from crm.leads.models import LeadCapture, LeadFormMapping
from crm.meta import services as meta

SYSTEM = Actor.system("lead_ads")
FORM_ID_LIMIT = 64
LEAD_CHANNELS = (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP)


# ---- form mappings ----------------------------------------------------------------------------


def list_mappings(workspace_id: uuid.UUID) -> list[LeadFormMapping]:
    return list(LeadFormMapping.objects.for_workspace(workspace_id).order_by("created_at"))


def get_mapping(workspace_id: uuid.UUID, mapping_id: uuid.UUID) -> LeadFormMapping:
    found = LeadFormMapping.objects.for_workspace(workspace_id).filter(pk=mapping_id).first()
    if found is None:
        raise NotFoundError("form mapping not found")
    return found


def _check(form_id: str, field_map: dict[str, str], tags: list[str], channels: list[str]) -> None:
    if not form_id.strip() or len(form_id) > FORM_ID_LIMIT:
        raise ValidationFailedError("invalid_form_id")
    if any(not str(k).strip() or not str(v).strip() for k, v in field_map.items()):
        raise ValidationFailedError("invalid_mapping")
    if not set(channels) <= set(LEAD_CHANNELS) or len(set(tags)) != len(tags):
        raise ValidationFailedError("invalid_mapping")


def save_mapping(
    workspace_id: uuid.UUID,
    *,
    form_id: str,
    name: str = "",
    field_map: dict[str, str] | None = None,
    tags: list[str] | None = None,
    consent_channels: list[str] | None = None,
) -> LeadFormMapping:
    """Create or replace the mapping of one form."""
    clean_map, clean_tags, channels = field_map or {}, tags or [], consent_channels or []
    _check(form_id, clean_map, clean_tags, channels)
    row, _ = LeadFormMapping.objects.update_or_create(
        workspace_id=workspace_id,
        form_id=form_id.strip(),
        defaults={
            "name": name.strip()[:100],
            "mapping": clean_map,
            "tags": clean_tags,
            "consent_channels": channels,
        },
    )
    return row


def delete_mapping(workspace_id: uuid.UUID, mapping_id: uuid.UUID) -> None:
    get_mapping(workspace_id, mapping_id).delete()


# ---- capture ---------------------------------------------------------------------------------


def list_captures(workspace_id: uuid.UUID, limit: int = 50) -> list[LeadCapture]:
    rows = LeadCapture.objects.for_workspace(workspace_id).select_related("contact")
    return list(rows[: max(1, min(limit, 200))])


def capture(
    workspace_id: uuid.UUID,
    *,
    leadgen_id: str,
    form_id: str = "",
    page_id: str = "",
    ad_id: str = "",
) -> LeadCapture | None:
    """Turn one lead ad submission into a contact. Delivered twice, it is stored once."""
    existing = LeadCapture.objects.for_workspace(workspace_id).filter(leadgen_id=leadgen_id)
    if existing.exists():
        return None
    lead = meta.fetch_lead(workspace_id, leadgen_id)
    form = str(lead.get("form_id") or form_id)
    form_map = LeadFormMapping.objects.for_workspace(workspace_id).filter(form_id=form).first()
    data, answers = mapping.to_contact_data(
        lead.get("field_data", []), form_map.mapping if form_map else {}
    )
    if not data.get("email") and not data.get("phone"):
        raise ValidationFailedError("lead_has_no_email_or_phone")
    ad = str(lead.get("ad_id") or ad_id)
    campaign = str(lead.get("campaign_id", ""))
    data["source"] = mapping.SOURCE
    data["attribution"] = {
        "channel": "meta_lead_ad",
        "form_id": form,
        "ad_id": ad,
        "adset_id": str(lead.get("adset_id", "")),
        "campaign_id": campaign,
    }
    try:
        with transaction.atomic():
            contact, created = contacts.upsert_contact(
                workspace_id=workspace_id, actor=SYSTEM, data=data
            )
            row = LeadCapture.objects.create(
                workspace_id=workspace_id,
                leadgen_id=leadgen_id,
                contact=contact,
                form_id=form,
                page_id=page_id,
                ad_id=ad,
                adset_id=str(lead.get("adset_id", "")),
                campaign_id=campaign,
                answers=answers,
            )
            _apply_form_rules(workspace_id, contact, form_map, leadgen_id)
            publish(
                "crm.lead.captured",
                workspace_id=workspace_id,
                actor=SYSTEM,
                data=events.LeadCaptured(
                    lead_capture_id=row.pk,
                    contact_id=contact.pk,
                    leadgen_id=leadgen_id,
                    form_id=form,
                    ad_id=ad,
                    campaign_id=campaign,
                    new_contact=created,
                ),
            )
    except IntegrityError as exc:  # a parallel delivery got there first
        raise ConflictError("lead already captured") from exc
    return row


def _apply_form_rules(
    workspace_id: uuid.UUID, contact: Any, form_map: LeadFormMapping | None, leadgen_id: str
) -> None:
    if form_map is None:
        return
    if form_map.tags:
        contacts.add_tags(
            workspace_id=workspace_id, actor=SYSTEM, contact=contact, names=list(form_map.tags)
        )
    for channel in form_map.consent_channels:
        consent.record(
            workspace_id=workspace_id,
            actor=SYSTEM,
            contact_id=contact.pk,
            channel=channel,
            purpose=Purpose.MARKETING,
            status=Status.OPTED_IN,
            source=Source.FORM,
            evidence={"lead_ad": leadgen_id, "form_id": form_map.form_id},
        )
