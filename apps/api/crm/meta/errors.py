"""Meta error codes mapped to our stable codes. Data only: add a row, not a branch."""

from __future__ import annotations

from dataclasses import dataclass

UNKNOWN = "meta_failed"
UNAVAILABLE = "meta_unavailable"
RATE_LIMITED = "meta_rate_limited"
UNKNOWN_OUTCOME = "meta_unknown_outcome"
NO_ACCOUNT = "meta_not_connected"
NO_SENDER = "meta_no_sender"
BAD_CREDENTIALS = "meta_token_invalid"
WINDOW_CLOSED = "window_closed"
HTTP_SERVER_ERROR_FROM = 500


@dataclass(frozen=True)
class Mapped:
    code: str
    retryable: bool


# Cloud API and Graph API error codes: https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
GRAPH: dict[int, Mapped] = {
    4: Mapped(RATE_LIMITED, True),
    17: Mapped(RATE_LIMITED, True),
    80007: Mapped(RATE_LIMITED, True),
    130429: Mapped(RATE_LIMITED, True),
    131056: Mapped(RATE_LIMITED, True),
    190: Mapped(BAD_CREDENTIALS, False),
    100: Mapped("meta_invalid_request", False),
    10: Mapped("meta_permission_denied", False),
    131026: Mapped("wa_undeliverable", False),
    131047: Mapped(WINDOW_CLOSED, False),
    131049: Mapped("wa_frequency_capped", False),
    131051: Mapped("wa_unsupported_type", False),
    131009: Mapped("meta_invalid_request", False),
    132000: Mapped("wa_template_params", False),
    132001: Mapped("wa_template_missing", False),
    132015: Mapped("wa_template_paused", False),
    132016: Mapped("wa_template_disabled", False),
    133010: Mapped("wa_number_not_registered", False),
    551: Mapped("msgr_recipient_unavailable", False),
    10901: Mapped(WINDOW_CLOSED, False),
}


def from_graph(code: int | None, http_status: int) -> Mapped:
    if code in GRAPH:
        return GRAPH[code]
    if http_status >= HTTP_SERVER_ERROR_FROM:
        return Mapped(UNAVAILABLE, True)
    return Mapped(UNKNOWN, False)
