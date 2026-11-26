"""Provider error codes mapped to our stable codes. Data only: add a row, not a branch."""

from __future__ import annotations

from dataclasses import dataclass

UNKNOWN = "sms_failed"
UNAVAILABLE = "sms_unavailable"
RATE_LIMITED = "sms_rate_limited"
NO_CREDENTIALS = "sms_no_credentials"
NO_SENDER = "sms_no_sender"
BAD_CREDENTIALS = "sms_credentials_invalid"


@dataclass(frozen=True)
class Mapped:
    code: str
    retryable: bool


# Twilio error codes: https://www.twilio.com/docs/api/errors
TWILIO: dict[int, Mapped] = {
    20003: Mapped(BAD_CREDENTIALS, False),
    20429: Mapped(RATE_LIMITED, True),
    21211: Mapped("sms_invalid_number", False),
    21408: Mapped("sms_region_blocked", False),
    21610: Mapped("sms_recipient_opted_out", False),
    21612: Mapped("sms_cannot_route", False),
    21614: Mapped("sms_not_mobile", False),
    30003: Mapped("sms_unreachable", False),
    30005: Mapped("sms_unknown_destination", False),
    30006: Mapped("sms_landline", False),
    30007: Mapped("sms_filtered", False),
    30008: Mapped(UNKNOWN, False),
}

HTTP_SERVER_ERROR_FROM = 500


def from_twilio(code: int | None, http_status: int) -> Mapped:
    if code in TWILIO:
        return TWILIO[code]
    if http_status >= HTTP_SERVER_ERROR_FROM:
        return Mapped(UNAVAILABLE, True)
    return Mapped(UNKNOWN, False)
