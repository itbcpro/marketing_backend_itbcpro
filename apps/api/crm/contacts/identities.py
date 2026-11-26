"""Identities: every address a contact is recognised by (email, phone, social ids)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from core.errors import ConflictError
from crm.contacts.models import Contact, ContactIdentity, IdentityChannel

ADDRESS_CHANNELS = (IdentityChannel.EMAIL, IdentityChannel.PHONE)


@dataclass
class Addresses:
    email: str = ""
    extra_emails: list[str] = field(default_factory=list)
    phone: str = ""
    extra_phones: list[str] = field(default_factory=list)


def addresses_of(contact: Contact, *, include_inactive: bool = False) -> Addresses:
    rows = ContactIdentity.objects.filter(contact=contact, channel__in=ADDRESS_CHANNELS)
    if not include_inactive:
        rows = rows.filter(is_active=True)
    result = Addresses()
    for row in rows.order_by("created_at", "pk"):
        primary, extras = (
            ("email", result.extra_emails)
            if row.channel == IdentityChannel.EMAIL
            else ("phone", result.extra_phones)
        )
        if row.is_primary:
            setattr(result, primary, row.value)
        else:
            extras.append(row.value)
    return result


def _wanted(
    email: str, extra_emails: list[str], phone: str, extra_phones: list[str]
) -> dict[tuple[str, str], bool]:
    wanted: dict[tuple[str, str], bool] = {}
    if email:
        wanted[(IdentityChannel.EMAIL, email)] = True
    for value in extra_emails:
        wanted.setdefault((IdentityChannel.EMAIL, value), False)
    if phone:
        wanted[(IdentityChannel.PHONE, phone)] = True
    for value in extra_phones:
        wanted.setdefault((IdentityChannel.PHONE, value), False)
    return wanted


def assert_free(
    workspace_id: uuid.UUID,
    contact: Contact | None,
    email: str,
    extra_emails: list[str],
    phone: str,
    extra_phones: list[str],
) -> None:
    """Fail when another active contact already owns one of the addresses."""
    wanted = _wanted(email, extra_emails, phone, extra_phones)
    if not wanted:
        return
    taken = ContactIdentity.objects.for_workspace(workspace_id).filter(
        is_active=True, value__in=[v for _, v in wanted]
    )
    if contact is not None:
        taken = taken.exclude(contact=contact)
    errors: dict[str, str] = {}
    for row in taken:
        if (row.channel, row.value) in wanted:
            errors[str(row.channel)] = "duplicate"
    if errors:
        raise ConflictError("address belongs to another contact", errors=errors)


def would_change(
    contact: Contact, email: str, extra_emails: list[str], phone: str, extra_phones: list[str]
) -> bool:
    now = addresses_of(contact)
    return (
        now.email != email
        or now.phone != phone
        or sorted(now.extra_emails) != sorted(extra_emails)
        or sorted(now.extra_phones) != sorted(extra_phones)
    )


def sync(
    contact: Contact, email: str, extra_emails: list[str], phone: str, extra_phones: list[str]
) -> None:
    """Make the email and phone identities of ``contact`` equal the wanted set."""
    wanted = _wanted(email, extra_emails, phone, extra_phones)
    existing = {
        (row.channel, row.value): row
        for row in ContactIdentity.objects.filter(
            contact=contact, channel__in=ADDRESS_CHANNELS, is_active=True
        )
    }
    for key, row in existing.items():
        if key not in wanted:
            row.delete()
        elif row.is_primary != wanted[key]:
            row.is_primary = wanted[key]
            row.save(update_fields=["is_primary", "updated_at"])
    for (channel, value), primary in wanted.items():
        if (channel, value) not in existing:
            ContactIdentity.objects.create(
                workspace_id=contact.workspace_id,
                contact=contact,
                channel=channel,
                value=value,
                is_primary=primary,
            )


def set_active(contact: Contact, *, active: bool) -> None:
    ContactIdentity.objects.filter(contact=contact).update(is_active=active)


def find_contact(workspace_id: uuid.UUID, channel: str, value: str) -> Contact | None:
    row = (
        ContactIdentity.objects.for_workspace(workspace_id)
        .filter(channel=channel, value=value, is_active=True, contact__deleted_at__isnull=True)
        .select_related("contact")
        .first()
    )
    return row.contact if row else None


def value_of(workspace_id: uuid.UUID, contact_id: uuid.UUID, channel: str) -> str:
    """The address to reach the contact on this identity channel, or an empty string."""
    row = (
        ContactIdentity.objects.for_workspace(workspace_id)
        .filter(contact_id=contact_id, channel=channel, is_active=True)
        .order_by("-is_primary", "created_at")
        .first()
    )
    return row.value if row else ""


def attach(contact: Contact, channel: str, value: str) -> ContactIdentity:
    """Add a non-address identity (a social id) to a contact; it must not belong to another."""
    existing = (
        ContactIdentity.objects.for_workspace(contact.workspace_id)
        .filter(channel=channel, value=value, is_active=True)
        .first()
    )
    if existing is not None:
        if existing.contact_id != contact.pk:
            raise ConflictError(
                "identity belongs to another contact", errors={str(channel): "duplicate"}
            )
        return existing
    return ContactIdentity.objects.create(
        workspace_id=contact.workspace_id, contact=contact, channel=channel, value=value
    )
