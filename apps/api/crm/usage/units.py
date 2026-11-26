"""How many billable units a message is: email is one, an SMS is counted in segments."""

from __future__ import annotations

import math

from crm.consent.models import Channel

# The GSM-7 basic alphabet (3GPP TS 23.038).
_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "ÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_BASIC = frozenset(_BASIC)
GSM_EXTENDED = frozenset("^{}\\[~]|€")  # each takes two characters of space
GSM_SINGLE, GSM_MULTI = 160, 153
UCS2_SINGLE, UCS2_MULTI = 70, 67


def sms_segments(text: str) -> int:
    """Segments of an SMS. Any character outside GSM-7 switches the whole message to UCS-2."""
    if not text:
        return 1
    if all(c in GSM_BASIC or c in GSM_EXTENDED for c in text):
        length = sum(2 if c in GSM_EXTENDED else 1 for c in text)
        single, multi = GSM_SINGLE, GSM_MULTI
    else:
        length = len(text.encode("utf-16-le")) // 2  # an emoji is two UTF-16 units
        single, multi = UCS2_SINGLE, UCS2_MULTI
    return 1 if length <= single else math.ceil(length / multi)


def units_for(channel: str, body: str) -> int:
    """Billable units of one outbound message on a channel."""
    return sms_segments(body) if channel == Channel.SMS else 1


SERVICE = "service"  # a free-form reply inside the window, priced apart from templates


def category_of(channel: str, attachments: list[dict[str, object]] | None) -> str:
    """Rate category: WhatsApp prices a template by its category and a reply as service."""
    if channel != Channel.WHATSAPP:
        return ""
    for item in attachments or []:
        if item.get("kind") == "template":
            return str(item.get("category", ""))
    return SERVICE
