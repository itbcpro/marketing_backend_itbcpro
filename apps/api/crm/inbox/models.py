"""Unified conversations: one thread per contact, every channel interleaved."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TenantManager, TenantModel
from crm.consent.models import Channel


class ChannelAccount(TenantModel):
    """A connected sender: a phone number, a mailbox, a WhatsApp number, a page. No secrets here."""

    channel = models.CharField(max_length=20, choices=Channel.choices)
    provider = models.CharField(max_length=50)
    external_id = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=20, default="active")
    config = models.JSONField(default=dict)

    objects = TenantManager["ChannelAccount"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "channel", "external_id"], name="uniq_channel_account"
            )
        ]


class ConversationStatus(models.TextChoices):
    OPEN = "open"
    CLOSED = "closed"


class Conversation(TenantModel):
    contact = models.OneToOneField(
        "crm_contacts.Contact", on_delete=models.CASCADE, related_name="conversation"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(
        max_length=10, choices=ConversationStatus.choices, default=ConversationStatus.OPEN
    )
    starred = models.BooleanField(default=False)
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_channel = models.CharField(max_length=20, blank=True, default="")
    last_preview = models.CharField(max_length=140, blank=True, default="")

    objects = TenantManager["Conversation"]()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=[s.value for s in ConversationStatus]),
                name="conversation_status_valid",
            )
        ]
        indexes = [
            models.Index(fields=["workspace", "-last_message_at", "-id"], name="inbox_order_idx"),
            models.Index(
                fields=["workspace", "assigned_to", "-last_message_at"], name="inbox_owner_idx"
            ),
        ]


class ConversationRead(TenantModel):
    """When a member last read a conversation; unread means newer inbound than this."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    read_at = models.DateTimeField()

    objects = TenantManager["ConversationRead"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["conversation", "user"], name="uniq_conversation_read")
        ]


class Direction(models.TextChoices):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class MessageStatus(models.TextChoices):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    BLOCKED = "blocked"
    RECEIVED = "received"


class Message(TenantModel):
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    channel = models.CharField(max_length=20, blank=True, default="")
    direction = models.CharField(max_length=10, choices=Direction.choices)
    channel_account = models.ForeignKey(
        ChannelAccount, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    provider_message_id = models.CharField(max_length=200, blank=True, default="")
    subject = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField(blank=True, default="")
    attachments = models.JSONField(default=list)
    # How this message is to be sent, such as {"track_opens": true}. Never shown to the customer.
    options = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=MessageStatus.choices)
    error_code = models.CharField(max_length=60, blank=True, default="")
    sent_by_type = models.CharField(max_length=20, blank=True, default="")
    sent_by_id = models.CharField(max_length=100, blank=True, default="")
    mentions = models.JSONField(default=list)
    idempotency_key = models.CharField(max_length=100, blank=True, default="")
    provider_ts = models.DateTimeField()

    objects = TenantManager["Message"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "channel", "provider_message_id"],
                condition=~Q(provider_message_id=""),
                name="uniq_message_provider_id",
            ),
            models.UniqueConstraint(
                fields=["workspace", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="uniq_message_idempotency_key",
            ),
            models.CheckConstraint(
                condition=Q(direction__in=[d.value for d in Direction])
                & Q(status__in=[s.value for s in MessageStatus]),
                name="message_enums_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "-provider_ts", "-id"], name="message_thread_idx")
        ]


class MessageStatusEvent(TenantModel):
    """Every status callback, kept so out-of-order or repeated callbacks can be audited."""

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="status_events")
    status = models.CharField(max_length=10)
    provider_ts = models.DateTimeField()
    error_code = models.CharField(max_length=60, blank=True, default="")

    objects = TenantManager["MessageStatusEvent"]()


class BridgeCursor(TenantModel):
    """How far a bridge has copied a source table, so each run only reads what is new."""

    source = models.CharField(max_length=30)
    last_created_at = models.DateTimeField(null=True, blank=True)
    last_id = models.UUIDField(null=True, blank=True)

    objects = TenantManager["BridgeCursor"]()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "source"], name="uniq_bridge_cursor")
        ]
