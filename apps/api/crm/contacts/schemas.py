"""Public response schemas of the contacts module."""

import uuid
from datetime import date, datetime
from typing import Any

from ninja import Schema


class ContactOut(Schema):
    id: uuid.UUID
    first_name: str
    last_name: str
    full_name: str
    email: str
    phone: str
    extra_emails: list[str]
    extra_phones: list[str]
    company_id: uuid.UUID | None
    assigned_to: uuid.UUID | None
    followers: list[uuid.UUID]
    contact_type: str
    source: str
    time_zone: str
    date_of_birth: date | None
    address: str
    city: str
    state: str
    postal_code: str
    country: str
    tags: list[str]
    custom: dict[str, Any]
    attribution: dict[str, Any]
    last_activity_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
