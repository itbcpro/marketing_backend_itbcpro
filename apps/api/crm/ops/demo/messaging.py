"""Senders, templates, conversations in every state, and campaigns with tracking."""

from __future__ import annotations

import uuid

from crm.campaigns import services as campaigns
from crm.campaigns.models import Campaign, CampaignRecipient
from crm.inbox import accounts, conversations
from crm.inbox.ingest import InboundMessage, ingest
from crm.inbox.models import ChannelAccount, Message
from crm.ops.demo.base import DEMO_TAG, Demo
from crm.templates import services as templates
from crm.templates.models import MessageTemplate
from crm.tracking.models import EmailEvent

SENDERS = [
    ("email", "smtp", "hello@demo.example.com", "Hello mailbox"),
    ("sms", "twilio", "+14165550100", "Main SMS line"),
    ("whatsapp", "meta", "+14165550101", "WhatsApp business"),
]
TEMPLATES = [
    (
        "Demo Welcome email",
        "template",
        "email",
        "Welcome, {{contact.first_name}}!",
        "Hi {{contact.first_name}},\n\nThanks for getting in touch. Book a time: "
        "{{workspace_values.booking_link}}\n\nITBCPro team",
    ),
    (
        "Demo Appointment reminder",
        "template",
        "sms",
        "",
        "Hi {{contact.first_name}}, a reminder of your visit tomorrow. Reply STOP to opt out.",
    ),
    (
        "Demo Monthly newsletter",
        "template",
        "email",
        "Your monthly update",
        "Hello {{contact.first_name}}, here is what is new this month.",
    ),
    (
        "Demo Quote follow-up",
        "template",
        "",
        "Following up on your quote",
        "Hi {{contact.first_name}}, did you get a chance to review the quote?",
    ),
    ("Demo Thanks snippet", "snippet", "", "", "Thanks for your message! We will reply shortly."),
    ("Demo Hours snippet", "snippet", "", "", "We are open Monday to Friday, 9am to 5pm."),
    (
        "Demo Booking snippet",
        "snippet",
        "",
        "",
        "You can book here: {{workspace_values.booking_link}}",
    ),
]
INBOUND = [
    "Hi, I saw your ad. What are your prices?",
    "Can I book a call for tomorrow?",
    "Thanks, got the quote. One question about the timeline.",
    "Is the offer still available?",
    "Please send me the invoice again.",
]
REPLIES = [
    "Thanks for reaching out! Here are the details.",
    "Sure, how about 3pm?",
    "Yes, it is still available.",
]
CONVERSATIONS = 24
UNSUB = "\n\nUnsubscribe: {{system.unsubscribe_url}}"
SENT_DELIVERY = ["delivered", "read", "delivered", "sent", "failed"]


def seed_senders(demo: Demo) -> dict[str, ChannelAccount]:
    found: dict[str, ChannelAccount] = {}
    for channel, provider, external, name in SENDERS:
        account = accounts.find_account(demo.wid, channel, external) or accounts.add_account(
            demo.wid, channel=channel, provider=provider, external_id=external, display_name=name
        )
        found[channel] = account
    return found


def seed_templates(demo: Demo) -> list[MessageTemplate]:
    rows = []
    for name, kind, channel, subject, body in TEMPLATES:
        row = MessageTemplate.objects.for_workspace(demo.wid).filter(
            name=name, archived_at__isnull=True
        ).first() or templates.create(
            workspace_id=demo.wid,
            actor=demo.actor,
            name=name,
            body=body,
            kind=kind,
            channel=channel,
            subject=subject,
        )
        rows.append(row)
    demo.count("templates", len(rows))
    return rows


def _stamp(demo: Demo, days: float, key: str, suffix: str) -> tuple[str, object]:
    return f"demo-{key}-{suffix}", demo.ago(days=days)


def seed_conversations(demo: Demo, senders: dict[str, ChannelAccount]) -> None:
    if (
        Message.objects.for_workspace(demo.wid)
        .filter(provider_message_id__startswith="demo-")
        .exists()
    ):
        return
    rng = demo.rng
    for index, contact in enumerate(demo.contacts[:CONVERSATIONS]):
        channel = ("email", "sms", "whatsapp")[index % 3]
        account = senders[channel]
        sender = ("email", contact.email) if channel == "email" else ("phone", contact.phone)
        days = 12 - index * 0.5
        result = ingest(
            workspace_id=demo.wid,
            item=InboundMessage(
                channel=channel,
                provider_message_id=f"demo-in-{index}",
                sender_channel=sender[0],
                sender_value=sender[1],
                provider_ts=demo.ago(days=days),
                body=rng.choice(INBOUND),
                subject="Question" if channel == "email" else "",
                channel_account_id=account.pk,
            ),
        )
        demo.count("messages")
        if index % 5 == 4:
            continue  # left unanswered: shows as unread
        out = conversations.create_outbound(
            workspace_id=demo.wid,
            actor=demo.actor,
            contact=contact,
            channel=channel,
            body=rng.choice(REPLIES),
            subject="Re: Question" if channel == "email" else "",
            channel_account_id=account.pk,
        )
        provider_id = f"demo-out-{index}"
        conversations.mark_sent(
            workspace_id=demo.wid,
            message_id=out.pk,
            channel=channel,
            provider_message_id=provider_id,
        )
        status = SENT_DELIVERY[index % len(SENT_DELIVERY)]
        if status != "sent":
            conversations.apply_status(
                workspace_id=demo.wid,
                channel=channel,
                provider_message_id=provider_id,
                status=status,
                provider_ts=demo.ago(days=days - 0.1),
                error_code="provider_rejected" if status == "failed" else "",
            )
        demo.count("messages")
        if index % 3 == 0:
            conversations.add_comment(
                workspace_id=demo.wid,
                actor=demo.actor,
                conversation_id=result.message.conversation_id,
                body="Internal: customer is on the Growth plan, handle with priority.",
            )
        conversations.set_flags(
            workspace_id=demo.wid,
            conversation_id=result.message.conversation_id,
            starred=index % 6 == 0,
            status="closed" if index % 7 == 0 else None,
        )
        if index % 2 == 0:
            conversations.mark_read(
                workspace_id=demo.wid,
                conversation_id=result.message.conversation_id,
                user_id=demo.owner.user_id,
            )


def _campaign(demo: Demo, name: str, **kwargs: object) -> Campaign:
    found = Campaign.objects.for_workspace(demo.wid).filter(name=name).first()
    if found:
        return found
    demo.count("campaigns")
    return campaigns.create(
        membership=demo.owner,
        actor=demo.actor,
        name=name,
        purpose="marketing",
        audience={
            "op": "and",
            "conditions": [{"field": "tags", "operator": "has", "value": DEMO_TAG}],
        },
        **kwargs,  # type: ignore[arg-type]
    )


def seed_campaigns(demo: Demo, senders: dict[str, ChannelAccount]) -> None:
    email_id = senders["email"].pk
    _campaign(
        demo,
        "Demo Draft: spring promo",
        channel="email",
        subject="Spring offer",
        body="Hi {{contact.first_name}}, spring offer inside." + UNSUB,
        channel_account_id=email_id,
    )
    later = _campaign(
        demo,
        "Demo Scheduled: product launch",
        channel="email",
        subject="We launched!",
        body="Big news, {{contact.first_name}}." + UNSUB,
        channel_account_id=email_id,
        track_opens=True,
        track_clicks=True,
    )
    if later.status == "draft":
        campaigns.launch(
            workspace_id=demo.wid,
            actor=demo.actor,
            campaign_id=later.pk,
            scheduled_at=demo.ahead(days=3),
        )
    done = _campaign(
        demo,
        "Demo Done: A/B newsletter",
        channel="email",
        subject="Newsletter A",
        body="Hello {{contact.first_name}}, newsletter A." + UNSUB,
        channel_account_id=email_id,
        track_opens=True,
        track_clicks=True,
        variant_subject="Newsletter B",
        variant_body="Hello {{contact.first_name}}, newsletter B." + UNSUB,
        variant_percent=50,
    )
    if not CampaignRecipient.objects.filter(campaign=done).exists():
        _finish_campaign(demo, done, email_id)


def _finish_campaign(demo: Demo, campaign: Campaign, account_id: uuid.UUID) -> None:
    rng = demo.rng
    for index, contact in enumerate(demo.contacts[:30]):
        variant = "B" if index % 2 else "A"
        status, reason, message = "sent", "", None
        if index % 10 == 9:
            status, reason = "blocked", "no_consent"
        elif index % 10 == 8:
            status, reason = "skipped", "suppressed"
        else:
            message = Message.objects.create(
                workspace_id=demo.wid,
                conversation=conversations.get_or_create(demo.wid, contact),
                channel="email",
                direction="outbound",
                channel_account_id=account_id,
                provider_message_id=f"demo-camp-{index}",
                subject=f"Newsletter {variant}",
                body="Newsletter body",
                status=rng.choice(["delivered", "read", "delivered"]),
                sent_by_type="system",
                sent_by_id="campaign",
                provider_ts=demo.ago(days=6),
            )
            if rng.random() < 0.6:
                EmailEvent.objects.create(
                    workspace_id=demo.wid, message_id=message.pk, contact_id=contact.pk, kind="open"
                )
            if rng.random() < 0.25:
                EmailEvent.objects.create(
                    workspace_id=demo.wid,
                    message_id=message.pk,
                    contact_id=contact.pk,
                    kind="click",
                    url="https://example.com/offer",
                )
        CampaignRecipient.objects.create(
            workspace_id=demo.wid,
            campaign=campaign,
            contact=contact,
            variant=variant,
            status=status,
            reason=reason,
            message=message,
            processed_at=demo.ago(days=6),
        )
    Campaign.objects.filter(pk=campaign.pk).update(
        status="done",
        started_at=demo.ago(days=6),
        finished_at=demo.ago(days=6, hours=-1),
    )


def run(demo: Demo) -> None:
    senders = seed_senders(demo)
    seed_templates(demo)
    seed_conversations(demo, senders)
    seed_campaigns(demo, senders)
