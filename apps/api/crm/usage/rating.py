"""Finding the rate of a unit and applying the organization's markup."""

from __future__ import annotations

from datetime import datetime

import phonenumbers
from django.db.models import Q

from crm.usage.models import RateCard

PERCENT = 100


def country_of(phone: str, declared: str = "") -> str:
    """Two-letter country of a recipient: the E.164 number wins, then the contact's own country."""
    if phone:
        try:
            region = phonenumbers.region_code_for_number(phonenumbers.parse(phone))
        except phonenumbers.NumberParseException:
            region = None
        if region:
            return region
    return declared.upper()


def find_rate(
    *, channel: str, currency: str, at: datetime, country: str = "", category: str = ""
) -> RateCard | None:
    """The newest applicable row; a row naming the country or category beats a general one."""
    rows = RateCard.objects.filter(
        channel=channel, currency=currency, effective_from__lte=at
    ).filter(Q(country=country) | Q(country=""), Q(category=category) | Q(category=""))
    newest: dict[tuple[str, str], RateCard] = {}
    for row in rows.order_by("-effective_from"):
        newest.setdefault((row.country, row.category), row)

    def specificity(row: RateCard) -> tuple[int, datetime]:
        return (2 * bool(row.country) + bool(row.category), row.effective_from)

    return max(newest.values(), key=specificity, default=None)


def with_markup(price_micros: int, percent: int) -> int:
    """Add the organization's markup, rounding up to the next micro."""
    return -(-price_micros * (PERCENT + percent) // PERCENT)
