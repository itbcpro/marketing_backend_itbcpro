"""Sender registration as data: the steps a number needs before carriers let it send.

The wizard in the web app renders whatever this table lists for the workspace's country, so a new
country or a changed carrier rule is a data change. Canada is the default.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_COUNTRY = "CA"


@dataclass(frozen=True)
class Step:
    key: str
    required: bool = True


class Status:
    NOT_STARTED = "not_started"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


STATUSES = (Status.NOT_STARTED, Status.PENDING, Status.APPROVED, Status.REJECTED)

STEPS: dict[str, tuple[Step, ...]] = {
    "CA": (
        Step("business_profile"),
        Step("sender_type"),
        Step("use_case"),
        Step("consent_evidence"),
        Step("sample_messages"),
        Step("provider_submission"),
    ),
    "US": (
        Step("business_profile"),
        Step("brand_registration"),
        Step("campaign_registration"),
        Step("consent_evidence"),
        Step("provider_submission"),
    ),
}


def steps_for(country: str) -> tuple[Step, ...]:
    return STEPS.get(country.upper(), STEPS[DEFAULT_COUNTRY])
