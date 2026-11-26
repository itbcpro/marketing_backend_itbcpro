"""Consent ledger: append-only events plus a derived current state per contact, channel, purpose."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TenantManager, TenantModel


class Channel(models.TextChoices):
    SMS = "sms"
    EMAIL = "email"
    VOICE = "voice"
    WHATSAPP = "whatsapp"
    MESSENGER = "messenger"
    INSTAGRAM = "instagram"


class Purpose(models.TextChoices):
    MARKETING = "marketing"
    TRANSACTIONAL = "transactional"


class Status(models.TextChoices):
    OPTED_IN = "opted_in"
    OPTED_OUT = "opted_out"


class Source(models.TextChoices):
    FORM = "form"
    KEYWORD = "keyword"
    LINK = "link"
    MANUAL = "manual"
    API = "api"
    IMPORT = "import"
    INBOUND_MESSAGE = "inbound_message"
    MERGE = "merge"


def default_law_profile() -> str:
    return str(settings.CRM_DEFAULT_LAW_PROFILE)


class ConsentSettings(TenantModel):
    """Per-workspace legal profile. The profile itself is data in ``crm.consent.profiles``."""

    law_profile = models.CharField(max_length=10, default=default_law_profile)
    quiet_hours_enabled = models.BooleanField(default=True)

    objects = TenantManager["ConsentSettings"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace"], name="uniq_consent_settings_workspace")
        ]


class ConsentEvent(TenantModel):
    """One recorded decision. Never updated or deleted by application code."""

    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="consent_events"
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    status = models.CharField(max_length=10, choices=Status.choices)
    source = models.CharField(max_length=20, choices=Source.choices)
    evidence = models.JSONField(default=dict)
    expires_at = models.DateTimeField(null=True, blank=True)
    actor_type = models.CharField(max_length=20)
    actor_id = models.CharField(max_length=100)
    occurred_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager["ConsentEvent"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(channel__in=[c.value for c in Channel])
                & Q(purpose__in=[p.value for p in Purpose])
                & Q(status__in=[s.value for s in Status])
                & Q(source__in=[s.value for s in Source]),
                name="consent_event_enums_valid",
            )
        ]
        indexes = [
            models.Index(
                fields=["contact", "channel", "purpose", "-occurred_at"], name="consent_event_idx"
            )
        ]
        ordering = ["-occurred_at", "-id"]

    def save(self, *args: object, **kwargs: object) -> None:
        if not self._state.adding:
            raise RuntimeError("consent events are append-only")
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class ConsentState(TenantModel):
    """The current answer for one contact, channel and purpose (derived from the ledger)."""

    contact = models.ForeignKey(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="consent_states"
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    status = models.CharField(max_length=10, choices=Status.choices)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_event = models.ForeignKey(ConsentEvent, on_delete=models.CASCADE, related_name="+")
    changed_at = models.DateTimeField()

    objects = TenantManager["ConsentState"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "channel", "purpose"], name="uniq_consent_state"
            )
        ]
