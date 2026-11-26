"""Calls, sending domain, WhatsApp templates, wallet and usage, invoices, response-time data."""

from __future__ import annotations

from datetime import date

from crm.billing import services as billing
from crm.billing.models import Invoice
from crm.inbox.models import Message
from crm.meta.models import WhatsAppTemplate
from crm.ops.demo.base import Demo
from crm.reports.models import FirstResponse
from crm.sending import domains
from crm.sending.models import SendingDomain
from crm.usage import wallet
from crm.usage.models import RateCard, UsageRecord, Wallet
from crm.voice import services as voice
from crm.voice.models import Call
from workspaces.models import Workspace

MICROS = 1_000_000
LINE = "+14165550102"
WA_TEMPLATES = [
    ("welcome_message", "utility", "approved", ""),
    ("spring_promo", "marketing", "approved", ""),
    ("order_update", "utility", "pending", ""),
    ("holiday_sale", "marketing", "rejected", "Promotional content not allowed in this category."),
    ("verify_code", "authentication", "paused", ""),
]
CALLS = [
    ("inbound", "completed", 184, False),
    ("inbound", "missed", 0, True),
    ("outbound", "completed", 62, False),
    ("inbound", "missed", 0, False),
    ("outbound", "failed", 0, False),
    ("inbound", "completed", 420, False),
]


def seed_voice(demo: Demo) -> None:
    if Call.objects.for_workspace(demo.wid).exists():
        return
    if voice.find_line(demo.wid, LINE) is None:
        voice.add_line(demo.wid, number=LINE, label="Demo voice line")
    for index, (direction, status, seconds, voicemail) in enumerate(CALLS * 2):
        contact = demo.contacts[index]
        inbound = direction == "inbound"
        Call.objects.create(
            workspace_id=demo.wid,
            contact=contact,
            direction=direction,
            status=status,
            provider_call_id=f"demo-call-{index}",
            from_number=contact.phone if inbound else LINE,
            to_number=LINE if inbound else contact.phone,
            duration_seconds=seconds,
            voicemail=voicemail,
            recording_url="https://example.com/rec.mp3" if voicemail else "",
            recording_seconds=24 if voicemail else 0,
            transcript="Hi, please call me back about my quote." if voicemail else "",
            text_back_sent=status == "missed" and index % 2 == 1,
        )
        demo.count("calls")


def seed_sending(demo: Demo) -> None:
    if not SendingDomain.objects.for_workspace(demo.wid).exists():
        domains.add_domain(workspace_id=demo.wid, actor=demo.actor, domain="mail.demo-itbcpro.com")
        demo.count("sending domains")
    if WhatsAppTemplate.objects.for_workspace(demo.wid).exists():
        return
    for name, category, status, reason in WA_TEMPLATES:
        WhatsAppTemplate.objects.create(
            workspace_id=demo.wid,
            name=name,
            language="en",
            category=category,
            status=status,
            body="Hello {{1}}, this is a demo template.",
            examples=[["Alex"]],
            reason=reason,
            meta_template_id=f"demo-{name}",
            synced_at=demo.ago(hours=3),
        )
        demo.count("whatsapp templates")


def seed_billing(demo: Demo) -> None:
    organization = Workspace.objects.select_related("organization").get(pk=demo.wid).organization
    if (
        Wallet.objects.filter(organization=organization).exists()
        and UsageRecord.objects.filter(workspace_id=demo.wid).exists()
    ):
        return
    wallet.top_up(
        organization, amount_micros=50 * MICROS, reference="demo-topup-1", note="Demo top-up"
    )
    currency = demo.workspace.reporting_currency
    cards = {
        channel: RateCard.objects.filter(channel=channel).first()
        or RateCard.objects.create(
            channel=channel,
            currency=currency,
            cost_micros=cost,
            price_micros=price,
            effective_from=demo.ago(days=90),
        )
        for channel, cost, price in [
            ("sms", 7_000, 11_000),
            ("email", 100, 400),
            ("whatsapp", 20_000, 30_000),
        ]
    }
    messages = Message.objects.for_workspace(demo.wid).filter(direction="outbound")
    for message in messages[:30]:
        card = cards.get(message.channel)
        if card is None or hasattr(message, "usage"):
            continue
        UsageRecord.objects.get_or_create(
            message=message,
            defaults=dict(
                workspace_id=demo.wid,
                channel=message.channel,
                units=1,
                currency=card.currency,
                cost_micros=card.cost_micros,
                price_micros=card.price_micros,
                rate_card=card,
            ),
        )
        demo.count("usage records")
    today = date.today()
    first = today.replace(day=1)
    last_end = first.replace(day=1)
    previous_first = date(last_end.year - (last_end.month == 1), (last_end.month - 2) % 12 + 1, 1)
    invoice = billing.generate(organization, previous_first, last_end.replace(day=1))
    if invoice and not Invoice.objects.filter(pk=invoice.pk, status="paid").exists():
        demo.count("invoices")


def seed_response_times(demo: Demo) -> None:
    if FirstResponse.objects.for_workspace(demo.wid).exists():
        return
    for index, contact in enumerate(demo.contacts[:20]):
        seconds = [45, 120, 600, 1800, 7200, 90][index % 6]
        inbound_at = demo.ago(days=index % 14, hours=index % 5)
        FirstResponse.objects.create(
            workspace_id=demo.wid,
            conversation_id=contact.pk,
            contact_id=contact.pk,
            channel=("email", "sms", "whatsapp")[index % 3],
            inbound_at=inbound_at,
            responded_at=inbound_at.replace() if index % 7 == 6 else inbound_at,
            seconds=None if index % 7 == 6 else seconds,
            responder_type="human" if index % 4 else "agent",
            responder_id=str(demo.members[index % len(demo.members)].user_id),
        )
    demo.count("response-time rows", 20)


def run(demo: Demo) -> None:
    seed_voice(demo)
    seed_sending(demo)
    seed_billing(demo)
    seed_response_times(demo)
