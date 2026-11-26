"""One place that normalises identifiers: every module stores and matches these forms."""

import phonenumbers
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator, validate_email

EMAIL_MAX_LENGTH = 254
_URL_VALIDATOR = URLValidator(schemes=["http", "https"])


class InvalidValueError(ValueError):
    """Raised when a raw value cannot be normalised; the message is a stable machine code."""


def normalise_email(raw: str) -> str:
    value = raw.strip().lower()
    if not value or len(value) > EMAIL_MAX_LENGTH:
        raise InvalidValueError("invalid_email")
    try:
        validate_email(value)
    except DjangoValidationError as exc:
        raise InvalidValueError("invalid_email") from exc
    return value


def normalise_phone(raw: str, *, region: str | None = None) -> str:
    """Return the E.164 form, using the workspace default region for national numbers."""
    try:
        parsed = phonenumbers.parse(raw.strip(), region or settings.CRM_DEFAULT_PHONE_REGION)
    except phonenumbers.NumberParseException as exc:
        raise InvalidValueError("invalid_phone") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidValueError("invalid_phone")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalise_url(raw: str) -> str:
    value = raw.strip()
    try:
        _URL_VALIDATOR(value)
    except DjangoValidationError as exc:
        raise InvalidValueError("invalid_url") from exc
    return value
