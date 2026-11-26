"""Lead form answers to contact data. The standard question names are data, not branches."""

from __future__ import annotations

from typing import Any

# Question names Meta's forms use for its prefilled fields, and the contact field each fills.
STANDARD: dict[str, str] = {
    "full_name": "full_name",
    "first_name": "first_name",
    "last_name": "last_name",
    "email": "email",
    "phone_number": "phone",
    "phone": "phone",
    "city": "city",
    "state": "state",
    "province": "state",
    "zip_code": "postal_code",
    "post_code": "postal_code",
    "postal_code": "postal_code",
    "country": "country",
    "street_address": "address",
}
CUSTOM_PREFIX = "custom."
SOURCE = "meta_lead_ad"
ANSWER_LIMIT = 500


def to_contact_data(
    field_data: list[dict[str, Any]], mapping: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """(contact fields, the answers as given). Unmapped custom questions are kept as answers."""
    data: dict[str, Any] = {}
    custom: dict[str, str] = {}
    answers: dict[str, str] = {}
    for item in field_data:
        name = str(item.get("name", ""))
        values = item.get("values") or []
        value = str(values[0]).strip()[:ANSWER_LIMIT] if values else ""
        if not name or not value:
            continue
        answers[name] = value
        target = mapping.get(name) or STANDARD.get(name)
        if target == "full_name":
            first, _, last = value.partition(" ")
            data.setdefault("first_name", first)
            if last:
                data.setdefault("last_name", last.strip())
        elif target and target.startswith(CUSTOM_PREFIX):
            custom[target[len(CUSTOM_PREFIX) :]] = value
        elif target:
            data[target] = value
    if custom:
        data["custom"] = custom
    return data, answers
