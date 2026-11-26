"""Legal profiles as data. Adding a jurisdiction means adding an entry here, not changing code.

Every number and rule below is configuration of the consent engine, not logic: the engine only
reads this table. Profiles are product defaults and not legal advice; owners confirm them.
"""

from dataclasses import dataclass, field
from datetime import time

from crm.consent.models import Channel


@dataclass(frozen=True)
class QuietHours:
    """Local hours when messages must not be sent (``start`` to ``end``, may cross midnight)."""

    start: time
    end: time


@dataclass(frozen=True)
class LawProfile:
    key: str
    label: str
    # Channels where marketing needs a recorded opt-in. Others only need "not opted out".
    opt_in_required: frozenset[str]
    # Months an implied consent stays valid, by evidence kind (0 means no implied consent).
    implied_months: dict[str, int] = field(default_factory=dict)
    quiet_hours: dict[str, QuietHours] = field(default_factory=dict)


_ALL_CHANNELS = frozenset(c.value for c in Channel)
_EVENING = QuietHours(start=time(21, 0), end=time(8, 0))

PROFILES: dict[str, LawProfile] = {
    "ca": LawProfile(
        key="ca",
        label="Canada (CASL, PIPEDA)",
        opt_in_required=_ALL_CHANNELS,
        implied_months={"existing_customer": 24, "inquiry": 6},
        quiet_hours={Channel.SMS: _EVENING, Channel.VOICE: _EVENING},
    ),
    "us": LawProfile(
        key="us",
        label="United States (TCPA, CAN-SPAM)",
        opt_in_required=frozenset({Channel.SMS, Channel.VOICE, Channel.WHATSAPP}),
        quiet_hours={Channel.SMS: _EVENING, Channel.VOICE: _EVENING},
    ),
    "eu": LawProfile(
        key="eu",
        label="European Union (GDPR, ePrivacy)",
        opt_in_required=_ALL_CHANNELS,
    ),
}


def get_profile(key: str) -> LawProfile:
    try:
        return PROFILES[key]
    except KeyError as exc:
        raise KeyError(f"unknown law profile {key}") from exc
